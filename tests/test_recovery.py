import re
import subprocess

import pytest

from gamelearn import db
from gamelearn.models import Project, Session
from gamelearn.services.explanation_service import build_explanation_context
from gamelearn.services.git_service import GitService
from gamelearn.services.recovery_service import recovery_preview, recover_session


@pytest.fixture
def project(app, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    unity = root / "NestedUnity"
    for name in ("Assets", "Packages", "ProjectSettings"):
        (unity / name).mkdir(parents=True)
    code = unity / "Assets" / "Player.cs"
    code.write_text("class Player {}\n")
    git("add", ".")
    git("commit", "-m", "Baseline")
    code.write_text("class Player { int score; }\n")
    git("add", ".")
    git("commit", "-m", "Add score")
    code.write_text("class Player { int score = 1; }\n")
    (unity / "Assets" / "New.cs").write_text("class New {}\n")
    (unity / "Assets" / "Scene.unity").write_text("PRIVATE_SCENE_CONTENT\n")
    value = Project(name="Recovery", path=str(unity))
    db.session.add(value)
    db.session.commit()
    return value


def test_recovery_preserves_repo_and_teaching_boundaries(project):
    git = GitService()
    before = git.status(project.path)
    head = git.head_commit(project.path)
    commits, _, _, _, _ = recovery_preview(project, "")
    baseline = commits[-1]["hash"]
    _, _, _, records, fingerprint = recovery_preview(project, baseline)
    session = recover_session(project, baseline, fingerprint, "2025-01-02")
    assert session.status == "COMPLETED"
    assert session.recovery == {"estimated_date": "2025-01-02"}
    assert session.started_at == session.ended_at
    assert len(session.file_changes) == len(records) == 3
    assert [event.event_type for event in session.events] == ["SESSION_RECOVERED"]
    assert git.status(project.path) == before
    assert git.head_commit(project.path) == head
    for profile in ("learning", "tutor"):
        context = build_explanation_context(session, profile)
        assert "PRIVATE_SCENE_CONTENT" not in str(context)
        payload = context["current_session"]
        assert payload["duration_seconds"] is None
        assert payload["evidence_origin"] == "recovered_snapshot"
        assert payload["timeline"] == []


def test_changed_preview_and_invalid_date_do_not_save(project):
    _, baseline, _, _, fingerprint = recovery_preview(project, "")
    with pytest.raises(ValueError, match="future"):
        recover_session(project, baseline, fingerprint, "2999-01-01")
    from pathlib import Path
    (Path(project.path) / "Assets" / "Player.cs").write_text("class Player { int other; }\n")
    with pytest.raises(ValueError, match="changed"):
        recover_session(project, baseline, fingerprint)
    assert Session.query.count() == 0


def test_recovery_rejects_arbitrary_ref_archive_and_active_session(project):
    with pytest.raises(ValueError, match="recent commits"):
        recovery_preview(project, "--all")
    project.is_archive = True
    with pytest.raises(ValueError, match="local project"):
        recovery_preview(project, "")
    project.is_archive = False
    db.session.add(Session(project=project, start_commit_hash="a" * 40))
    db.session.commit()
    with pytest.raises(ValueError, match="active session"):
        recovery_preview(project, "")


def test_recovery_route_requires_review_and_csrf(client, app, project):
    url = f"/projects/{project.id}/recover"
    page = client.get(url)
    assert page.status_code == 200
    assert b"Recover a forgotten session" in page.data
    token = re.search(rb'name="preview_token" value="([^"]+)"', page.data)[1].decode()
    assert client.post(url, data={"preview_token": token}).status_code == 403
    csrf = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)[1].decode()
    assert csrf == app.config["CODEX_BRIDGE_CSRF_TOKEN"]
    client.post(url, data={"csrf_token": csrf, "preview_token": "tampered"})
    assert Session.query.count() == 0
    response = client.post(url, data={"csrf_token": csrf, "preview_token": token,
                                    "estimated_date": "2025-01-02"})
    assert response.status_code == 302
    session = Session.query.one()
    assert session.recovery is not None
    summary = client.get(response.location)
    assert b"Recovered session" in summary.data
    assert b"2025-01-02" in summary.data
    client.post(url, data={"csrf_token": csrf, "preview_token": token})
    assert Session.query.count() == 1


def test_empty_recovery_is_not_imported(project, monkeypatch):
    git = GitService()
    monkeypatch.setattr(git, "session_diff", lambda *args: [])
    _, baseline, _, records, fingerprint = recovery_preview(project, "", git)
    assert records == []
    with pytest.raises(ValueError, match="no changes"):
        recover_session(project, baseline, fingerprint, git_service=git)
    assert Session.query.count() == 0


def test_recovery_marker_survives_backup(project):
    from gamelearn.services.backup_service import session_payload, build_session
    _, baseline, _, _, fingerprint = recovery_preview(project, "")
    session = recover_session(project, baseline, fingerprint, "2025-01-02")
    restored, _ = build_session(session_payload(session))
    assert restored.recovery == session.recovery
    assert len(restored.file_changes) == len(session.file_changes)
