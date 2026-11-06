import pytest

from gamelearn import db
from gamelearn.models import Project, Session, utcnow
from gamelearn.services.explanation_service import build_explanation_context


@pytest.mark.parametrize("status,archive", [("ACTIVE", False), ("COMPLETED", False), ("COMPLETED", True)])
def test_prompt_can_be_added_updated_and_cleared(app, client, status, archive):
    session = Session(
        project=Project(name="Game", path="archive:test", is_archive=archive),
        start_commit_hash="a" * 40, status=status,
        ended_at=utcnow() if status == "COMPLETED" else None,
    )
    db.session.add(session)
    db.session.commit()
    assert session.prompt is None
    url = f"/sessions/{session.id}/prompt"
    page = f"/sessions/{session.id}" + ("/summary" if status == "COMPLETED" else "")
    token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
    assert b'(optional)' in client.get(page).data
    for prompt in ["Add jumping.\nKeep movement.", "</textarea><script>alert(1)</script>", "x" * 20000, "   "]:
        response = client.post(url, data={"csrf_token": token, "prompt": prompt})
        assert response.status_code == 302
        assert response.headers["Location"].endswith(page)
        db.session.expire_all()
        assert db.session.get(Session, session.id).prompt == (prompt if prompt.strip() else None)
        html = client.get(page).get_data(as_text=True)
        assert '<script>alert(1)</script>' not in html
        if prompt.startswith('</textarea>'):
            assert '&lt;/textarea&gt;&lt;script&gt;' in html
        assert session.status == status
        assert session.events == []
    session.prompt = 'Private saved prompt'
    assert 'prompt' not in build_explanation_context(session)['current_session']


def test_prompt_rejects_invalid_token_and_oversized_input(app, client):
    session = Session(project=Project(name="Game", path="archive:test"),
                      start_commit_hash="a" * 40, prompt="Original")
    db.session.add(session)
    db.session.commit()
    url = f"/sessions/{session.id}/prompt"
    for token in [None, "wrong"]:
        data = {"prompt": "Changed"}
        if token:
            data["csrf_token"] = token
        assert client.post(url, data=data).status_code == 403
    token = app.config["CODEX_BRIDGE_CSRF_TOKEN"]
    assert client.post(url, data={"csrf_token": token, "prompt": "x" * 20001}).status_code == 400
    db.session.expire_all()
    assert session.prompt == "Original"
    assert client.post('/sessions/99999/prompt', data={"csrf_token": token}).status_code == 404
