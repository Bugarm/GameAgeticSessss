from gamelearn.services.codex_bridge import (
    CodexAppServerClient,
    CodexBridgeError,
    CodexChatDispatcher,
    CodexExplanationDispatcher,
    ChatJob,
    ExplanationJob,
    _safe_error,
    LearningOptionsError,
    _default_learning_model,
    _learning_models,
)
import pytest


class FakeAppServerClient:
    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.on_wait = None
        self.account_result = {
            "account": {"type": "chatgpt", "planType": "plus"},
            "requiresOpenaiAuth": True,
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def initialize(self):
        self.calls.append(("initialize", None))

    def request(self, method, params, timeout_seconds=None):
        self.calls.append((method, params))
        self.timeouts.append(timeout_seconds)
        if method == "account/read":
            return self.account_result
        if method == "config/read":
            return {"config": {"model": "configured-model", "private": "/private/config"}}
        if method == "thread/start":
            return {"thread": {"id": "task-test"}, "model": "resolved-model", "reasoningEffort": "high"}
        if method == "turn/start":
            return {"turn": {"id": "turn-test"}}
        return {}

    def wait_for_turn(self, turn_id):
        self.calls.append(("wait_for_turn", turn_id))
        if self.on_wait:
            self.on_wait()
            return {"id": turn_id, "status": "interrupted"}, ""
        return {"id": turn_id, "status": "completed"}, "The learning page file was created."

    def interrupt_turn(self, thread_id, turn_id):
        self.calls.append(("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}))

    def terminate(self):
        self.calls.append(("terminate", None))


def test_learning_catalog_paginates_and_filters_private_values():
    class CatalogClient:
        def request(self, method, params, timeout_seconds=None):
            assert method == "model/list"
            if params["cursor"] is None:
                return {"data": [
                    {"model": "older", "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
                    {"model": "/private/model", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
                    {"model": "hidden", "hidden": True, "supportedReasoningEfforts": [{"reasoningEffort": "low"}]},
                ], "nextCursor": "page2"}
            return {"data": [{"model": "gpt-6-astra", "supportedReasoningEfforts": [
                {"reasoningEffort": "low"}, {"reasoningEffort": "high"}, {"reasoningEffort": "/private/path"}
            ]}]}
    models = _learning_models(CatalogClient())
    assert [item["model"] for item in models] == ["older", "gpt-6-astra"]
    assert models[1]["efforts"] == ["low", "high"]
    assert _default_learning_model(models) == "gpt-6-astra"
    assert _default_learning_model(models[:1]) == "older"


@pytest.mark.parametrize("model, effort", [(None, "low"), ("older", "high")])
def test_submit_passes_selected_learning_settings_to_cli(tmp_path, monkeypatch, model, effort):
    fake = FakeAppServerClient()
    dispatcher = CodexExplanationDispatcher(tmp_path, client_factory=lambda: fake, mermaid_validator=lambda *_: None)
    monkeypatch.setattr(dispatcher, "connection_payload", lambda: {
        "connected": True, "generation_model": "gpt-6-astra", "models": [
            {"model": "gpt-6-astra", "efforts": ["low", "high"]},
            {"model": "older", "efforts": ["low", "high"]},
        ]
    })
    class ImmediateThread:
        def __init__(self, target, args, **kwargs):
            self.run = lambda: target(*args)
        def start(self):
            self.run()
    monkeypatch.setattr("gamelearn.services.codex_bridge.threading.Thread", ImmediateThread)
    path = tmp_path / "work" / "lesson.html"
    path.parent.mkdir()
    job = dispatcher.submit(1, "fixed evidence", "lesson", path,
                            lambda text, dest: dest.write_text("lesson"), model=model, reasoning_effort=effort)
    assert job.status == "COMPLETED"
    start = next(params for method, params in fake.calls if method == "thread/start")
    turn = next(params for method, params in fake.calls if method == "turn/start")
    assert start["model"] == (model or "gpt-6-astra")
    assert start["config"]["model_reasoning_effort"] == effort
    assert turn["effort"] == effort
    assert job.payload()["reasoning_effort"] == effort


@pytest.mark.parametrize("model, effort", [("unknown", "low"), ("gpt-6-astra", "unsupported"), ([], "low")])
def test_invalid_learning_choices_preserve_existing_page(tmp_path, monkeypatch, model, effort):
    dispatcher = CodexExplanationDispatcher(tmp_path)
    monkeypatch.setattr(dispatcher, "connection_payload", lambda: {
        "connected": True, "generation_model": "gpt-6-astra", "models": []
    })
    path = tmp_path / "work" / "lesson.html"
    path.parent.mkdir()
    path.write_text("existing lesson")
    with pytest.raises(LearningOptionsError):
        dispatcher.submit(1, "evidence", "lesson", path, lambda *_: None, model=model, reasoning_effort=effort)
    assert path.read_text() == "existing lesson"
    assert dispatcher.status(1) is None


def test_dispatcher_creates_named_dedicated_task_with_fixed_project_root(tmp_path):
    fake_client = FakeAppServerClient()
    dispatcher = CodexExplanationDispatcher(
        tmp_path,
        client_factory=lambda: fake_client,
    )
    output_path = tmp_path / "work" / "gamelearn-session-7.html"
    output_path.parent.mkdir()
    job = ExplanationJob(
        session_id=7,
        task_title="GameLearn Â· Game Â· Session 7",
        output_path=str(output_path),
    )

    dispatcher._run(
        job,
        "fixed server-side explanation prompt",
        lambda final_text, destination: destination.write_text(
            "<div>Learning page</div>", encoding="utf-8"
        ),
    )

    assert job.status == "COMPLETED"
    assert job.task_id == "task-test"
    assert job.turn_id == "turn-test"
    assert job.payload()["model"] == "resolved-model"
    assert job.payload()["reasoning_effort"] == "low"
    assert "task_url" not in job.payload()
    assert "task_id" not in job.payload()
    assert "turn_id" not in job.payload()
    assert set(job.payload()["phase_durations"]) == {
        "app_server_startup",
        "codex_generation",
        "manifest_rendering",
        "mermaid_validation",
    }
    start = next(call for call in fake_client.calls if call[0] == "thread/start")
    assert start[1]["cwd"] == str(tmp_path.resolve())
    assert start[1]["approvalPolicy"] == "never"
    assert start[1]["sandbox"] == "readOnly"
    assert start[1]["config"]["model_reasoning_effort"] == "low"
    name = next(call for call in fake_client.calls if call[0] == "thread/name/set")
    assert name[1] == {"threadId": "task-test", "name": "GameLearn Â· Game Â· Session 7"}
    turn = next(call for call in fake_client.calls if call[0] == "turn/start")
    assert turn[1]["threadId"] == "task-test"
    assert turn[1]["input"] == [{"type": "text", "text": "fixed server-side explanation prompt"}]
    assert turn[1]["cwd"] == str(tmp_path.resolve())
    assert turn[1]["approvalPolicy"] == "never"
    assert turn[1]["effort"] == "low"


def test_dispatcher_sends_learning_manifest_output_schema(tmp_path):
    fake_client = FakeAppServerClient()
    dispatcher = CodexExplanationDispatcher(
        tmp_path,
        client_factory=lambda: fake_client,
    )
    output_path = tmp_path / "work" / "gamelearn-session-10.html"
    output_path.parent.mkdir()
    job = ExplanationJob(
        session_id=10,
        task_title="GameLearn Â· Game Â· Session 10",
        output_path=str(output_path),
    )
    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version"],
        "properties": {"schema_version": {"type": "integer", "const": 3}},
    }

    dispatcher._run(
        job,
        "fixed server-side explanation prompt",
        lambda final_text, destination: destination.write_text(
            "<div>Learning page</div>", encoding="utf-8"
        ),
        output_schema=output_schema,
    )

    turn = next(call for call in fake_client.calls if call[0] == "turn/start")
    assert turn[1]["outputSchema"] == output_schema


def test_raw_invalid_schema_error_is_not_exposed_to_students():
    raw_error = (
        '{"type":"error","error":{"code":"invalid_json_schema",'
        '"message":"private protocol details"},"status":400}'
    )

    message = _safe_error(CodexBridgeError(raw_error))

    assert message == (
        "Codex rejected GameLearn's structured lesson format. "
        "Restart GameLearn and try creating the page again."
    )
    assert "private protocol details" not in message


def test_dispatcher_fails_when_renderer_does_not_write_learning_page(tmp_path):
    fake_client = FakeAppServerClient()
    fake_client.wait_for_turn = lambda turn_id: (
        {"id": turn_id, "status": "completed"},
        "The learning page file was created.",
    )
    dispatcher = CodexExplanationDispatcher(
        tmp_path,
        client_factory=lambda: fake_client,
    )
    job = ExplanationJob(
        session_id=8,
        task_title="GameLearn Â· Game Â· Session 8",
        output_path=str(tmp_path / "missing.html"),
    )

    dispatcher._run(job, "fixed server-side explanation prompt", lambda *_: None)

    assert job.status == "FAILED"
    assert "could not render the generated lesson content" in job.message


def test_dispatcher_rejects_and_removes_invalid_mermaid_page(tmp_path):
    fake_client = FakeAppServerClient()
    output_path = tmp_path / "work" / "gamelearn-session-9.html"
    output_path.parent.mkdir()
    def reject_mermaid(path, project_root):
        assert path == output_path
        assert project_root == str(tmp_path.resolve())
        raise CodexBridgeError("The generated learning page contained invalid Mermaid diagram syntax.")

    dispatcher = CodexExplanationDispatcher(
        tmp_path,
        client_factory=lambda: fake_client,
        mermaid_validator=reject_mermaid,
    )
    job = ExplanationJob(
        session_id=9,
        task_title="GameLearn Â· Game Â· Session 9",
        output_path=str(output_path),
    )

    dispatcher._run(
        job,
        "fixed server-side explanation prompt",
        lambda final_text, destination: destination.write_text(
            '<div class="mermaid">flowchart TD\nA[Broken --&gt; B[Node]</div>',
            encoding="utf-8",
        ),
    )

    assert job.status == "FAILED"
    assert "invalid Mermaid diagram syntax" in job.message
    assert not output_path.exists()


def test_app_server_allows_long_learning_page_turns():
    client = CodexAppServerClient()

    assert client.turn_timeout_seconds == 900


def test_app_server_reports_structured_turn_progress_without_exposing_content():
    client = CodexAppServerClient()
    events = []
    client.set_status_callback(events.append)
    client.messages.put(
        {
            "method": "item/started",
            "params": {"item": {"type": "reasoning", "text": "private reasoning"}},
        }
    )
    client.messages.put(
        {
            "method": "item/started",
            "params": {"item": {"type": "fileChange", "path": "private-path.html"}},
        }
    )
    client.messages.put(
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "private preamble"}},
        }
    )
    client.messages.put(
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "text": "private final text"}},
        }
    )
    client.messages.put(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "last": {
                        "inputTokens": 1200,
                        "cachedInputTokens": 400,
                        "outputTokens": 80,
                        "reasoningOutputTokens": 25,
                        "totalTokens": 1305,
                    }
                }
            },
        }
    )
    client.messages.put(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-progress", "status": "completed"}},
        }
    )

    completion, final_text = client.wait_for_turn("turn-progress")

    assert completion["status"] == "completed"
    assert final_text == "private final text"
    assert events == [
        {
            "type": "turn_progress",
            "stage": "planning",
            "activity": "Started planning the lesson structure.",
        },
        {
            "type": "turn_progress",
            "stage": "checking",
            "activity": "Started a lesson-content preparation step.",
        },
        {
            "type": "turn_progress",
            "stage": "finishing",
            "activity": "Received the task completion report.",
        },
        {
            "type": "turn_progress",
            "stage": "finishing",
            "activity": "Received the task completion report.",
        },
        {
            "type": "token_usage",
            "usage": {
                "inputTokens": 1200,
                "cachedInputTokens": 400,
                "outputTokens": 80,
                "reasoningOutputTokens": 25,
                "totalTokens": 1305,
            },
        },
    ]
    assert "private reasoning" not in str(events)
    assert "private-path.html" not in str(events)
    assert "private preamble" not in str(events)


def test_dispatcher_progress_messages_do_not_move_backwards(tmp_path):
    dispatcher = CodexExplanationDispatcher(tmp_path, client_factory=FakeAppServerClient)
    job = ExplanationJob(session_id=17)

    dispatcher._handle_client_status(job, {"type": "turn_progress", "stage": "writing"})
    dispatcher._handle_client_status(job, {"type": "turn_progress", "stage": "planning"})
    dispatcher._handle_client_status(
        job,
        {
            "type": "token_usage",
            "usage": {"inputTokens": 900, "cachedInputTokens": 300, "outputTokens": 40},
        },
    )

    assert job.progress_percent == 74
    assert job.message == "Codex is assembling the lesson content."
    assert job.payload()["progress_percent"] == 74
    assert job.payload()["activity_count"] == 2
    assert job.payload()["token_usage"]["cachedInputTokens"] == 300


def test_dispatcher_interrupts_and_cancels_only_the_active_job(tmp_path):
    fake_client = FakeAppServerClient()
    dispatcher = CodexExplanationDispatcher(tmp_path, client_factory=lambda: fake_client)
    output_path = tmp_path / "work" / "gamelearn-session-9.html"
    output_path.parent.mkdir()
    job = ExplanationJob(
        session_id=9,
        task_title="GameLearn Â· Game Â· Session 9",
        output_path=str(output_path),
    )
    dispatcher._jobs[job.session_id] = job
    fake_client.on_wait = lambda: dispatcher.stop(job.session_id)

    dispatcher._run(
        job,
        "fixed server-side explanation prompt",
        lambda final_text, destination: destination.write_text(
            "<div>Learning page</div>", encoding="utf-8"
        ),
    )

    assert job.status == "CANCELLED"
    assert job.message == "Learning-page creation was stopped."
    interrupt = next(call for call in fake_client.calls if call[0] == "turn/interrupt")
    assert interrupt[1] == {"threadId": "task-test", "turnId": "turn-test"}


def test_app_server_client_reports_and_caps_connection_retries():
    retry_line = (
        '{"level":"WARN","fields":{"message":"stream connection failed; waiting to retry",'
        '"retry_delay":"60s"},"target":"codex_core::responses_retry"}'
    )

    class RetryProcess:
        def __init__(self):
            self.stderr = iter([retry_line, retry_line])
            self.terminated = False

        def poll(self):
            return None if not self.terminated else 1

        def terminate(self):
            self.terminated = True

    process = RetryProcess()
    events = []
    client = CodexAppServerClient(max_connection_failures=2)
    client.process = process
    client.set_status_callback(events.append)

    client._read_stderr()

    assert events == [
        {"type": "connection_retry", "attempt": 1, "maximum": 2, "retry_delay": "60s"},
        {"type": "connection_retry", "attempt": 2, "maximum": 2, "retry_delay": "60s"},
    ]
    assert process.terminated is True
    assert "after 2 attempts" in client._process_error()


def test_dispatcher_disables_generation_when_codex_cli_is_signed_out(tmp_path):
    fake_client = FakeAppServerClient()
    fake_client.account_result = {"account": None, "requiresOpenaiAuth": True}
    dispatcher = CodexExplanationDispatcher(tmp_path, client_factory=lambda: fake_client)

    payload = dispatcher.connection_payload()

    assert payload["connected"] is False
    assert "codex login --device-auth" in payload["message"]


def test_dispatcher_allows_generation_when_codex_cli_is_authenticated(tmp_path):
    fake_client = FakeAppServerClient()
    dispatcher = CodexExplanationDispatcher(tmp_path, client_factory=lambda: fake_client)

    payload = dispatcher.connection_payload()

    assert payload == {
        "connected": True, "message": "Ready to create the learning page.",
        "model": "configured-model", "reasoning_effort": "low",
        "models": [], "generation_model": "gpt-6-astra",
    }
    assert fake_client.timeouts[-1] == 15


def test_model_preview_unavailable_does_not_disable_generation(tmp_path):
    class OlderClient(FakeAppServerClient):
        def request(self, method, params, timeout_seconds=None):
            if method == "config/read":
                raise CodexBridgeError("Unsupported method")
            return super().request(method, params, timeout_seconds)

    dispatcher = CodexExplanationDispatcher(tmp_path, client_factory=OlderClient)
    payload = dispatcher.connection_payload()
    assert payload["connected"] is True
    assert payload["model"] is None
    assert payload["reasoning_effort"] == "low"


def test_model_preview_does_not_expose_private_config(tmp_path):
    class PrivateClient(FakeAppServerClient):
        def request(self, method, params, timeout_seconds=None):
            if method == "config/read":
                return {"config": {"model": "C:/private/model", "secret": "private-token"}}
            return super().request(method, params, timeout_seconds)

    payload = CodexExplanationDispatcher(tmp_path, client_factory=PrivateClient).connection_payload()
    assert payload["model"] is None
    assert "private" not in str(payload)


def test_dispatcher_does_not_misreport_app_server_failure_as_signed_out(tmp_path):
    class FailingAppServerClient:
        def __enter__(self):
            raise OSError("App Server process could not start")

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    dispatcher = CodexExplanationDispatcher(
        tmp_path, client_factory=FailingAppServerClient
    )

    payload = dispatcher.connection_payload()

    assert payload["connected"] is False
    assert "App Server" in payload["message"]
    assert "does not mean the CLI is signed out" in payload["message"]
    assert "restarting Codex" not in payload["message"]


def test_chat_dispatcher_creates_a_new_read_only_task_and_returns_answer(tmp_path, monkeypatch):
    fake_client = FakeAppServerClient()
    dispatcher = CodexChatDispatcher(tmp_path, client_factory=lambda: fake_client)
    results = []
    monkeypatch.setattr("gamelearn.services.codex_bridge.find_codex_command", lambda: ["codex"])
    job = ChatJob(message_id=12)

    dispatcher._run(
        job,
        "fixed tutor prompt",
        "GameLearn tutor Â· Session 1 Â· Chat 2",
        lambda status, content, task_id: results.append((status, content, task_id)),
    )

    assert job.status == "COMPLETED"
    assert results == [("COMPLETED", "The learning page file was created.", "task-test")]
    start = next(call for call in fake_client.calls if call[0] == "thread/start")
    assert start[1]["sandbox"] == "readOnly"
    assert start[1]["approvalPolicy"] == "never"
    turn = next(call for call in fake_client.calls if call[0] == "turn/start")
    assert turn[1]["input"] == [{"type": "text", "text": "fixed tutor prompt"}]
    assert turn[1]["effort"] == "low"
