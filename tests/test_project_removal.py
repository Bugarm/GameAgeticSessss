from unittest.mock import patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from gamelearn import db
from gamelearn.models import Project, Session, Event, SessionFileChange, ChatConversation, ChatMessage
from gamelearn.services.backup_service import page_path
from gamelearn.services.codex_bridge import ExplanationJob


def saved_project(tmp_path):
    project = Project(name="Remove me", path=str(tmp_path / "Unity"))
    session = Session(project=project, status="COMPLETED", start_commit_hash="a" * 40)
    session.events.append(Event(source="SYSTEM", event_type="SESSION_STARTED", title="Started"))
    session.file_changes.append(SessionFileChange(path="Assets/Player.cs", change_type="MODIFIED"))
    chat = ChatConversation(session=session, title="Question")
    chat.messages.append(ChatMessage(role="tutor", content="Answer"))
    db.session.add(project)
    db.session.commit()
    return project, session


def remove(client, app, project_id, **overrides):
    data = {"csrf_token": app.config["CODEX_BRIDGE_CSRF_TOKEN"], "confirm": "remove"}
    data.update(overrides)
    return client.post(f"/projects/{project_id}/remove", data=data)


def test_removal_cascades_and_preserves_unity_and_other_project(app, client, tmp_path):
    project, session = saved_project(tmp_path)
    project_id, session_id = project.id, session.id
    unity = tmp_path / "Unity"
    unity.mkdir()
    source = unity / "Player.cs"
    source.write_text("student work", encoding="utf-8")
    other = Project(name="Keep me", path=str(tmp_path / "Other"))
    db.session.add(other)
    db.session.commit()
    page = page_path(session.id)
    page.parent.mkdir()
    page.write_text("lesson", encoding="utf-8")
    dispatcher = app.extensions["codex_explanations"]
    dispatcher._jobs[session_id] = ExplanationJob(session_id=session_id, status="COMPLETED")

    assert client.get(f"/projects/{project_id}/remove").status_code == 200
    assert db.session.get(Project, project_id) is not None
    response = remove(client, app, project_id)
    assert response.status_code == 302
    assert Project.query.one().name == "Keep me"
    for model in (Session, Event, SessionFileChange, ChatConversation, ChatMessage):
        assert model.query.count() == 0
    assert not page.exists()
    assert not list(page.parent.iterdir())
    assert dispatcher.status(session_id) is None
    assert source.read_text(encoding="utf-8") == "student work"
    assert client.get(f"/sessions/{session_id}").status_code == 404


@pytest.mark.parametrize("overrides,code", [({"csrf_token": ""}, 403), ({"confirm": ""}, 400)])
def test_removal_requires_token_and_confirmation(app, client, tmp_path, overrides, code):
    project, _ = saved_project(tmp_path)
    assert remove(client, app, project.id, **overrides).status_code == code
    assert Project.query.count() == 1


@pytest.mark.parametrize("busy", ["session", "lesson", "tutor"])
def test_removal_blocks_active_work(app, client, tmp_path, busy):
    project, session = saved_project(tmp_path)
    if busy == "session":
        session.status = "ACTIVE"
    elif busy == "tutor":
        session.chat_conversations[0].messages[0].status = "PENDING"
    else:
        app.extensions["codex_explanations"]._jobs[session.id] = ExplanationJob(session_id=session.id, status="RUNNING")
    db.session.commit()
    assert remove(client, app, project.id).status_code == 409
    assert Project.query.count() == 1
    assert Session.query.count() == 1


def test_removal_restores_page_on_database_failure(app, client, tmp_path):
    project, session = saved_project(tmp_path)
    page = page_path(session.id)
    page.parent.mkdir()
    page.write_text("lesson", encoding="utf-8")
    with patch.object(db.session, "commit", side_effect=SQLAlchemyError("failed")):
        assert remove(client, app, project.id).status_code == 500
    assert Project.query.count() == 1
    assert page.read_text(encoding="utf-8") == "lesson"


def test_remove_empty_archive_and_missing_project(app, client):
    project = Project(name="Archive", path="archive:example", is_archive=True)
    db.session.add(project)
    db.session.commit()
    project_id = project.id
    assert remove(client, app, project_id).status_code == 302
    assert remove(client, app, project_id).status_code == 404
