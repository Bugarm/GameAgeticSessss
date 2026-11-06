from datetime import timedelta
from pathlib import Path
import subprocess
import pytest

from gamelearn import db
from gamelearn.models import ChatConversation, ChatMessage, Event, Project, Session, SessionFileChange, utcnow
from gamelearn.services.codex_bridge import ExplanationJob


class FakeCodexDispatcher:
    def __init__(self):
        self.jobs = {}
        self.prompts = []
        self.task_titles = []
        self.output_paths = []
        self.artifact_builders = []
        self.output_schemas = []

    def connection_payload(self):
        return {"connected": True, "message": "Ready for test Codex task."}

    def submit(
        self,
        session_id,
        prompt,
        task_title,
        output_path,
        artifact_builder,
        output_schema=None,
        model=None,
        reasoning_effort="low",
    ):
        self.prompts.append(prompt)
        self.task_titles.append(task_title)
        self.output_paths.append(str(output_path))
        self.artifact_builders.append(artifact_builder)
        self.output_schemas.append(output_schema)
        job = ExplanationJob(
            session_id=session_id,
            model=model,
            reasoning_effort=reasoning_effort,
            status="QUEUED",
            task_title=task_title,
            output_path=str(output_path),
            input_characters=len(prompt),
        )
        self.jobs[session_id] = job
        return job

    def status(self, session_id):
        return self.jobs.get(session_id)

    def stop(self, session_id):
        job = self.jobs[session_id]
        job.status = "CANCELLED"
        job.message = "Learning-page creation was stopped."
        return job


class FakeCodexChatDispatcher:
    def __init__(self):
        self.prompts = []
        self.titles = []

    def submit(self, message_id, prompt, task_title, completion_callback):
        self.prompts.append(prompt)
        self.titles.append(task_title)
        completion_callback("COMPLETED", f"Saved tutor answer {len(self.prompts)}", f"private-task-{message_id}")
        return object()

def test_session_events_endpoint_returns_structured_evidence(app, client, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="a" * 40)
        event = Event(
            session=session,
            source="SYSTEM",
            event_type="SESSION_STARTED",
            title="Session started",
            status="SUCCESS",
        )
        db.session.add_all([project, session, event])
        db.session.commit()
        session_id = session.id

    response = client.get(f"/api/sessions/{session_id}/events")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["events"][0]["event_type"] == "SESSION_STARTED"
    assert payload["session_status"] == "ACTIVE"


def test_session_timer_timestamp_retains_utc_after_database_reload(app, client, tmp_path):
    import re
    from datetime import datetime, timezone

    started = utcnow()
    with app.app_context():
        project = Project(name="Timer", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="a" * 40, started_at=started)
        db.session.add(session)
        db.session.commit()
        session_id = session.id

    response = client.get(f"/sessions/{session_id}")
    assert response.status_code == 200
    timestamp = re.search(r'data-started-at="([^"]+)"', response.get_data(as_text=True)).group(1)
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == started.astimezone(timezone.utc)


def test_registration_discovers_nested_unity_project(app, client, tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    nested = tmp_path / "student-game" / "MyPlatformer"
    for name in ("Assets", "Packages", "ProjectSettings"):
        (nested / name).mkdir(parents=True, exist_ok=True)
    response = client.post(
        "/projects",
        data={"path": str(tmp_path)},
    )

    assert response.status_code == 302
    with app.app_context():
        project = Project.query.one()
        assert project.path == str(nested.resolve())


def test_explanation_context_compares_with_previous_completed_session(app, client, tmp_path):
    now = utcnow()
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        previous = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="b" * 40,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            status="COMPLETED",
        )
        previous.file_changes.append(
            SessionFileChange(
                path="Assets/Player.cs",
                change_type="MODIFIED",
                additions=2,
                deletions=1,
                diff_text="previous diff",
            )
        )
        previous.file_changes.append(
            SessionFileChange(
                path="Assets/OldOnly.cs",
                change_type="MODIFIED",
                additions=50,
                deletions=10,
                diff_text="unrelated previous-session diff",
            )
        )
        current = Session(
            project=project,
            start_commit_hash="b" * 40,
            end_commit_hash="c" * 40,
            started_at=now,
            ended_at=now + timedelta(minutes=20),
            status="COMPLETED",
        )
        current.file_changes.extend(
            [
                SessionFileChange(
                    path="Assets/Player.cs",
                    change_type="MODIFIED",
                    additions=5,
                    deletions=2,
                    diff_text="current diff",
                ),
                SessionFileChange(
                    path="Assets/Enemy.cs",
                    change_type="CREATED",
                    additions=10,
                    deletions=0,
                    diff_text="enemy diff",
                ),
            ]
        )
        db.session.add(project)
        db.session.commit()
        current_id = current.id
        previous_id = previous.id

    response = client.get(f"/api/sessions/{current_id}/explanation-context")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["current_session"]["id"] == current_id
    assert payload["previous_session"]["id"] == previous_id
    assert payload["comparison"]["files_in_both"] == ["Assets/Player.cs"]
    assert payload["comparison"]["files_only_in_current"] == ["Assets/Enemy.cs"]
    assert payload["comparison"]["files_only_in_previous"] == ["Assets/OldOnly.cs"]
    assert [item["path"] for item in payload["previous_session"]["file_changes"]] == [
        "Assets/Player.cs"
    ]
    assert payload["current_session"]["file_changes"][0]["unified_diff"] == "enemy diff"


def test_first_session_explanation_has_no_previous_baseline(app, client, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.get(f"/api/sessions/{session_id}/explanation-context")

    payload = response.get_json()
    assert payload["previous_session"] is None
    assert payload["comparison"]["baseline"] == "none_available"


def test_explanation_context_sends_only_code_diffs_and_code_timeline(
    app, client, tmp_path
):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="b" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        session.file_changes.extend(
            [
                SessionFileChange(
                    path="Assets/Scripts/SceneHarness.cs",
                    change_type="CREATED",
                    additions=4,
                    deletions=0,
                    diff_text="+ public class SceneHarness {}",
                ),
                SessionFileChange(
                    path="Assets/Scenes/Lesson.unity",
                    change_type="MODIFIED",
                    additions=600,
                    deletions=400,
                    diff_text="+ serialized scene contents that must not be sent",
                ),
                SessionFileChange(
                    path="Logs/unity-build.log",
                    change_type="CREATED",
                    additions=600,
                    deletions=0,
                    diff_text="+ routine engine chatter that must not be sent",
                ),
            ]
        )
        for _ in range(25):
            session.events.extend(
                [
                    Event(
                        source="GIT",
                        event_type="FILE_CREATED",
                        title="File created",
                        description="Assets/Scripts/SceneHarness.cs",
                        status="INFO",
                        metadata_json='{"path":"Assets/Scripts/SceneHarness.cs"}',
                    ),
                    Event(
                        source="GIT",
                        event_type="FILE_CHANGED",
                        title="File changed",
                        description="Assets/Scenes/Lesson.unity",
                        status="INFO",
                        metadata_json='{"path":"Assets/Scenes/Lesson.unity"}',
                    ),
                    Event(
                        source="GIT",
                        event_type="GIT_STATUS",
                        title="Changes detected",
                        description="3 changed files observed.",
                        status="INFO",
                    ),
                ]
            )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.get(f"/api/sessions/{session_id}/explanation-context")
    response_text = response.get_data(as_text=True)
    payload = response.get_json()

    assert payload["evidence_limits"]["profile"] == "learning"
    assert payload["evidence_limits"]["code_diffs_only"] is True
    assert payload["evidence_limits"]["non_code_contents_included"] is False
    assert [change["path"] for change in payload["current_session"]["file_changes"]] == [
        "Assets/Scripts/SceneHarness.cs"
    ]
    assert payload["current_session"]["file_changes"][0]["unified_diff"] == (
        "+ public class SceneHarness {}"
    )
    assert len(payload["current_session"]["timeline"]) == 1
    assert payload["current_session"]["timeline"][0]["repeat_count"] == 25
    assert payload["current_session"]["timeline"][0]["path"] == (
        "Assets/Scripts/SceneHarness.cs"
    )
    non_code = payload["current_session"]["non_code_change_summary"]
    assert non_code["count"] == 2
    assert non_code["by_extension"] == {".log": 1, ".unity": 1}
    assert non_code["contents_included"] is False
    assert "serialized scene contents" not in response_text
    assert "routine engine chatter" not in response_text


def test_summary_contains_learning_page_controls_without_raw_evidence_link(app, client, tmp_path):
    app.extensions["codex_explanations"] = FakeCodexDispatcher()
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.get(f"/sessions/{session_id}/summary")

    assert response.status_code == 200
    assert b"Create learning page" in response.data
    assert f'href="/api/sessions/{session_id}/explanation-context"'.encode() not in response.data
    assert b"View evidence JSON" not in response.data
    assert f"/api/sessions/{session_id}/explain".encode() in response.data
    assert f"/api/sessions/{session_id}/explanation-stop".encode() in response.data
    assert b'id="stop-explanation-button"' in response.data
    assert b"Open Codex task" not in response.data
    assert b'id="explain-progress"' in response.data
    assert b'id="explain-telemetry"' in response.data
    assert b'id="explain-activity-log"' in response.data
    assert b"Ready for test Codex task." in response.data


def test_summary_offers_to_recreate_an_existing_learning_page(app, client, tmp_path):
    app.extensions["codex_explanations"] = FakeCodexDispatcher()
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        output = Path(app.config["LEARNING_PAGE_ROOT"]) / f"gamelearn-session-{session_id}.html"
        output.parent.mkdir(parents=True)
        output.write_text("<section>Existing lesson</section>", encoding="utf-8")

    response = client.get(f"/sessions/{session_id}/summary")

    assert response.status_code == 200
    assert b"Recreate learning page" in response.data


@pytest.mark.parametrize("options", [{}, {"model": "gpt-6-astra", "reasoning_effort": "high"}])
def test_explain_session_creates_fixed_dedicated_codex_task(app, client, tmp_path, options):
    dispatcher = FakeCodexDispatcher()
    app.extensions["codex_explanations"] = dispatcher
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        session.file_changes.append(
            SessionFileChange(
                path="Assets/Player.cs",
                change_type="MODIFIED",
                additions=1,
                deletions=0,
                diff_text="@@ -1 +1,2 @@\n class Player {}\n+// lesson change\n",
            )
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]

    response = client.post(
        f"/api/sessions/{session_id}/explain",
        headers={"X-GameLearn-Token": token},
        json=options,
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["status"] == "QUEUED"
    assert payload["model"] == options.get("model")
    assert payload["reasoning_effort"] == options.get("reasoning_effort", "low")
    assert "task_id" not in payload
    assert "turn_id" not in payload
    assert payload["estimated_input_tokens"] > 0
    assert payload["learning_page_url"] == f"/sessions/{session_id}/learning-page"
    assert len(dispatcher.prompts) == 1
    assert "Use $" not in dispatcher.prompts[0]
    assert "Return exactly one JSON object" in dispatcher.prompts[0]
    assert "exactly four meaningful teaching steps" in dispatcher.prompts[0]
    assert "GAMELEARN_EVIDENCE_START" in dispatcher.prompts[0]
    assert "current_session.file_changes code diffs" in dispatcher.prompts[0]
    assert "non_code_change_summary" in dispatcher.prompts[0]
    assert "why_agent_probably_did_this" in dispatcher.prompts[0]
    assert "Return schema_version 5" in dispatcher.prompts[0]
    assert "snippet_references" in dispatcher.prompts[0]
    assert "complete stored block" in dispatcher.prompts[0]
    assert "Never copy code or line numbers into the JSON" in dispatcher.prompts[0]
    assert '"snippets"' not in dispatcher.prompts[0]
    assert "Mermaid flowchart" in dispatcher.prompts[0]
    assert "accTitle:" in dispatcher.prompts[0]
    assert "GameLearn validates Mermaid once" in dispatcher.prompts[0]
    assert "no Markdown, commentary, HTML, CSS, JavaScript" in dispatcher.prompts[0]
    assert "Do not include answer keys, rubrics" in dispatcher.prompts[0]
    assert "never ask the student to design, create, edit, run, implement, build" in dispatcher.prompts[0]
    assert "first item must be open" in dispatcher.prompts[0]
    assert "at least two multiple_choice items" in dispatcher.prompts[0]
    assert f'"id":{session_id}' in dispatcher.prompts[0]
    assert dispatcher.task_titles == [f"GameLearn · Game · Session {session_id}"]
    assert Path(dispatcher.output_paths[0]).name == f"gamelearn-session-{session_id}.html"
    assert dispatcher.output_schemas[0]["properties"]["schema_version"] == {
        "type": "integer",
        "const": 5,
    }
    assert dispatcher.output_schemas[0]["additionalProperties"] is False


@pytest.mark.parametrize("options", [[], {"model": []}, {"reasoning_effort": None}, {"prompt": "arbitrary"}, {"task_id": "arbitrary"}])
def test_explain_rejects_invalid_options_without_dispatch(app, client, tmp_path, options):
    dispatcher = FakeCodexDispatcher()
    app.extensions["codex_explanations"] = dispatcher
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="a" * 40, ended_at=utcnow(), status="COMPLETED")
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
    response = client.post(f"/api/sessions/{session_id}/explain", json=options,
                           headers={"X-GameLearn-Token": token})
    assert response.status_code == 400
    assert dispatcher.prompts == []


def test_explain_session_rejects_cross_origin_form_style_request(app, client, tmp_path):
    app.extensions["codex_explanations"] = FakeCodexDispatcher()
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.post(f"/api/sessions/{session_id}/explain")

    assert response.status_code == 403
    assert app.extensions["codex_explanations"].prompts == []


def test_stop_explanation_cancels_only_the_session_job(app, client, tmp_path):
    dispatcher = FakeCodexDispatcher()
    app.extensions["codex_explanations"] = dispatcher
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
    dispatcher.submit(session_id, "prompt", "title", tmp_path / "page.html", lambda *_: None)

    response = client.post(
        f"/api/sessions/{session_id}/explanation-stop",
        headers={"X-GameLearn-Token": token},
    )

    assert response.status_code == 202
    assert response.get_json()["status"] == "CANCELLED"


def test_stop_explanation_requires_local_request_token(app, client, tmp_path):
    app.extensions["codex_explanations"] = FakeCodexDispatcher()
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.post(f"/api/sessions/{session_id}/explanation-stop")

    assert response.status_code == 403


def test_learning_page_route_hosts_generated_fragment_in_sandbox(app, client, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        output_path = Path(app.config["LEARNING_PAGE_ROOT"]) / f"gamelearn-session-{session_id}.html"
        output_path.parent.mkdir(parents=True)
        output_path.write_text("<section id=\"lesson\"><h1>Generated lesson</h1></section>", encoding="utf-8")

    response = client.get(f"/sessions/{session_id}/learning-page")

    assert response.status_code == 200
    assert b'sandbox="allow-scripts"' in response.data
    assert b"Generated lesson" in response.data
    assert b"default-src &#39;none&#39;" in response.data
    assert b"mermaid-11.12.1.min.js" in response.data
    assert b"renderVisibleMermaid" in response.data
    assert b"diagram.getClientRects().length" in response.data
    assert b"new MutationObserver" in response.data
    assert b"nodes: diagrams" in response.data
    assert b"securityLevel:" in response.data
    assert b"strict" in response.data
    assert b'id="learning-chat"' in response.data
    assert f"/api/sessions/{session_id}/chat/messages".encode() in response.data
    assert f"/api/sessions/{session_id}/open-code-file".encode() in response.data
    assert b"gamelearn:tutor-question" in response.data
    assert b"gamelearn:open-file" in response.data
    assert b"sendFollowUpMessage" in response.data
    assert b'id="learning-file-toast"' in response.data
    assert b"syntax-keyword" in response.data
    assert b"gl-code-line-highlight" in response.data
    assert b"gamelearnHighlightLines" in response.data
    assert b"data-gamelearn-worksheet" in response.data
    assert b"normalizeFileControl" in response.data
    assert b"submitWorksheet" in response.data
    assert b"toggleWorksheetHint" in response.data
    assert b"data-gamelearn-hint-toggle" in response.data
    assert b"button[type=&#39;submit&#39;]" in response.data
    assert b"Hide hint" in response.data
    assert b"max-height: 32rem" in response.data
    assert b"overflow-x: hidden" in response.data
    assert b"data-gamelearn-step-jump" in response.data
    assert b"button.addEventListener" in response.data
    assert b"repeat(5, minmax(0, 1fr))" in response.data
    assert b"minmax(14rem, 19rem)" not in response.data
    assert b"normalizeOpenWorksheetQuestions" not in response.data
    assert b"Explain what the changed code does" not in response.data
    assert b"Opening a new tutor chat to check your answer" in response.data
    assert b"gamelearn:worksheet-submission" in response.data
    assert b"gamelearn:tutor-chat-opened" in response.data
    assert b"Give explanatory feedback using this evidence-backed criterion" not in response.data

    mermaid_response = client.get("/static/vendor/mermaid-11.12.1.min.js")
    assert mermaid_response.status_code == 200
    assert len(mermaid_response.data) > 1_000_000


def test_open_code_file_endpoint_opens_only_a_recorded_session_path(
    app, client, tmp_path, monkeypatch
):
    opened = []
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="b" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        session.file_changes.append(
            SessionFileChange(path="Assets/Player.cs", change_type="MODIFIED")
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
        output = Path(app.config["LEARNING_PAGE_ROOT"]) / f"gamelearn-session-{session_id}.html"
        output.parent.mkdir(parents=True)
        output.write_text("<section>Lesson</section>", encoding="utf-8")

    def fake_open(session, path):
        assert path == "Assets/Player.cs"
        opened.append(path)
        return Path("Assets/Player.cs")

    monkeypatch.setattr("gamelearn.routes.open_session_file", fake_open)
    response = client.post(
        f"/api/sessions/{session_id}/open-code-file",
        json={"path": "Assets/Player.cs"},
        headers={"X-GameLearn-Token": token},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "opened": True,
        "path": "Assets/Player.cs",
        "filename": "Player.cs",
    }
    assert opened == ["Assets/Player.cs"]


def test_open_code_file_endpoint_requires_local_token(app, client, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.post(
        f"/api/sessions/{session_id}/open-code-file", json={"path": "Assets/Player.cs"}
    )

    assert response.status_code == 403


def test_learning_chat_persists_history_and_uses_a_fixed_new_task_request(app, client, tmp_path):
    dispatcher = FakeCodexChatDispatcher()
    app.extensions["codex_chat"] = dispatcher
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="b" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        session.file_changes.append(
            SessionFileChange(
                path="Assets/Player.cs",
                change_type="MODIFIED",
                additions=1,
                deletions=0,
                diff_text="+ speed = 5;",
            )
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
        output = Path(app.config["LEARNING_PAGE_ROOT"]) / f"gamelearn-session-{session_id}.html"
        output.parent.mkdir(parents=True)
        output.write_text("<section>Lesson</section>", encoding="utf-8")

    first = client.post(
        f"/api/sessions/{session_id}/chat/messages",
        json={"question": "Why was speed added?"},
        headers={"X-GameLearn-Token": token},
    )
    assert first.status_code == 202
    first_payload = first.get_json()
    conversation_id = first_payload["conversation"]["id"]

    second = client.post(
        f"/api/sessions/{session_id}/chat/messages",
        json={"question": "Can you explain that more simply?", "conversation_id": conversation_id},
        headers={"X-GameLearn-Token": token},
    )
    assert second.status_code == 202
    assert len(dispatcher.prompts) == 2
    assert "GAMELEARN_TUTOR_DATA_START" in dispatcher.prompts[1]
    assert '"profile":"tutor"' in dispatcher.prompts[1]
    assert "assume a coding agent—not a human developer—made the code changes" in dispatcher.prompts[1]
    assert "agent's reasoning" in dispatcher.prompts[1]
    assert "Why was speed added?" in dispatcher.prompts[1]
    assert "Saved tutor answer 1" in dispatcher.prompts[1]
    assert "private-task" not in second.get_data(as_text=True)

    saved = client.get(f"/api/sessions/{session_id}/chats").get_json()["conversations"]
    assert saved[0]["id"] == conversation_id
    assert [message["role"] for message in saved[0]["messages"]] == [
        "student", "tutor", "student", "tutor"
    ]
    assert saved[0]["messages"][-1]["content"] == "Saved tutor answer 2"
    with app.app_context():
        assert ChatConversation.query.count() == 1
        assert ChatMessage.query.count() == 4
        assert ChatMessage.query.filter_by(role="tutor").first().codex_task_id.startswith("private-task-")


def test_worksheet_feedback_keeps_private_prompt_out_of_saved_student_message(
    app, client, tmp_path
):
    dispatcher = FakeCodexChatDispatcher()
    app.extensions["codex_chat"] = dispatcher
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="b" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        session.file_changes.append(
            SessionFileChange(
                path="Assets/Player.cs",
                change_type="MODIFIED",
                additions=1,
                deletions=0,
                diff_text="+ speed = 5;",
            )
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
        output = Path(app.config["LEARNING_PAGE_ROOT"]) / f"gamelearn-session-{session_id}.html"
        output.parent.mkdir(parents=True)
        output.write_text("<section>Lesson</section>", encoding="utf-8")

    complete_question = "Why does the player now use a speed value of 5?"
    complete_answer = "It makes movement use the recorded speed rather than an unstated default."
    response = client.post(
        f"/api/sessions/{session_id}/chat/messages",
        json={
            "worksheet": {
                "question": complete_question,
                "student_answer": complete_answer,
            }
        },
        headers={"X-GameLearn-Token": token},
    )

    assert response.status_code == 202
    payload = response.get_json()
    visible_message = payload["conversation"]["messages"][0]["content"]
    assert visible_message == f"{complete_question}\n\nStudent answer: {complete_answer}"
    assert "Evaluate the answer yourself" not in visible_message
    assert "private instruction" not in visible_message
    assert len(dispatcher.prompts) == 1
    private_prompt = dispatcher.prompts[0]
    assert "GAMELEARN_WORKSHEET_DATA_START" in private_prompt
    assert complete_question in private_prompt
    assert complete_answer in private_prompt
    assert "+ speed = 5;" in private_prompt
    assert "brand-new Codex task with no earlier chat history" in private_prompt
    assert "Evaluate the answer yourself" in private_prompt
    assert "assume a coding agent—not a human developer—made the code changes" in private_prompt


def test_legacy_worksheet_private_prompt_is_hidden_from_chat_history(app, client, tmp_path):
    dispatcher = FakeCodexChatDispatcher()
    app.extensions["codex_chat"] = dispatcher
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="a" * 40, status="COMPLETED")
        conversation = ChatConversation(session=session, title="Question Student answer: camera Give …")
        legacy_content = (
            "Why are both roots removed?\n\nStudent answer: To avoid stale objects.\n\n"
            "Give explanatory feedback using this evidence-backed criterion: hidden rubric."
        )
        conversation.messages.append(
            ChatMessage(role="student", content=legacy_content, status="COMPLETED")
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        conversation_id = conversation.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
        output = Path(app.config["LEARNING_PAGE_ROOT"]) / f"gamelearn-session-{session_id}.html"
        output.parent.mkdir(parents=True)
        output.write_text("<section>Lesson</section>", encoding="utf-8")

    payload = client.get(f"/api/sessions/{session_id}/chats").get_json()["conversations"][0]

    assert payload["title"] == "Why are both roots removed?"
    assert payload["messages"][0]["content"] == (
        "Why are both roots removed?\n\nStudent answer: To avoid stale objects."
    )
    assert "hidden rubric" not in payload["messages"][0]["content"]

    continued = client.post(
        f"/api/sessions/{session_id}/chat/messages",
        json={"question": "Can you explain that feedback?", "conversation_id": conversation_id},
        headers={"X-GameLearn-Token": token},
    )

    assert continued.status_code == 202
    assert "To avoid stale objects." in dispatcher.prompts[0]
    assert "hidden rubric" not in dispatcher.prompts[0]


def test_learning_chat_requires_local_token_and_ready_page(app, client, tmp_path):
    app.extensions["codex_chat"] = FakeCodexChatDispatcher()
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id
        token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]

    missing_token = client.post(
        f"/api/sessions/{session_id}/chat/messages", json={"question": "Why?"}
    )
    assert missing_token.status_code == 403
    missing_page = client.post(
        f"/api/sessions/{session_id}/chat/messages",
        json={"question": "Why?"},
        headers={"X-GameLearn-Token": token},
    )
    assert missing_page.status_code == 409


def test_learning_page_route_returns_to_summary_until_file_exists(app, client, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(
            project=project,
            start_commit_hash="a" * 40,
            end_commit_hash="a" * 40,
            ended_at=utcnow(),
            status="COMPLETED",
        )
        db.session.add(project)
        db.session.commit()
        session_id = session.id

    response = client.get(f"/sessions/{session_id}/learning-page")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/sessions/{session_id}/summary")
