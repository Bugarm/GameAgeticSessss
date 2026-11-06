from gamelearn.services.session_service import find_unity_projects, validate_unity_project
from gamelearn import db
from gamelearn.models import Event, Project, Session
from gamelearn.services.git_service import GitChange, GitDiffRecord
from gamelearn.services.session_service import end_session, inspect_session, start_session


def test_validate_unity_project_accepts_expected_directories(tmp_path):
    for name in ("Assets", "Packages", "ProjectSettings"):
        (tmp_path / name).mkdir()

    detected, missing = validate_unity_project(tmp_path)

    assert detected is True
    assert missing == []


def test_validate_unity_project_reports_missing_directories(tmp_path):
    (tmp_path / "Assets").mkdir()

    detected, missing = validate_unity_project(tmp_path)

    assert detected is False
    assert missing == ["Packages", "ProjectSettings"]


class FakeGit:
    def __init__(self, changes=None):
        self.changes = changes or []

    def require_repository(self, path):
        return None

    def status(self, path):
        return self.changes

    def head_commit(self, path):
        return "a" * 40

    def repository_root(self, path):
        return path

    def session_diff(self, path, start_commit):
        return [GitDiffRecord("Assets/Test.cs", "MODIFIED", 2, 1, "diff text")]


def test_session_start_records_baseline_and_event(app, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        db.session.add(project)
        db.session.commit()

        session, changes = start_session(project, FakeGit())

        assert changes == []
        assert session.status == "ACTIVE"
        assert session.start_commit_hash == "a" * 40
        assert Event.query.filter_by(session_id=session.id, event_type="SESSION_STARTED").count() == 1


def test_session_does_not_start_from_dirty_tree(app, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        db.session.add(project)
        db.session.commit()
        dirty = [GitChange("Assets/Test.cs", "MODIFIED", " ", "M")]

        session, changes = start_session(project, FakeGit(dirty))

        assert session is None
        assert changes == dirty
        assert Session.query.count() == 0


def test_duplicate_project_change_is_suppressed(app, tmp_path):
    with app.app_context():
        target = tmp_path / "Assets" / "Test.cs"
        target.parent.mkdir()
        target.write_text("class Test {}", encoding="utf-8")
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="a" * 40)
        db.session.add_all([project, session])
        db.session.commit()
        modified = [GitChange("Assets/Test.cs", "MODIFIED", " ", "M")]
        fake_git = FakeGit(modified)

        first = inspect_session(session, fake_git)
        second = inspect_session(session, fake_git)

        assert any(event.event_type == "FILE_CHANGED" for event in first)
        assert second == []
        assert Event.query.filter_by(session_id=session.id, event_type="FILE_CHANGED").count() == 1


def test_new_save_to_same_file_creates_new_event(app, tmp_path):
    with app.app_context():
        target = tmp_path / "Test.cs"
        target.write_text("one", encoding="utf-8")
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="a" * 40)
        db.session.add_all([project, session])
        db.session.commit()
        fake_git = FakeGit([GitChange("Test.cs", "MODIFIED", " ", "M")])
        inspect_session(session, fake_git)

        target.write_text("a longer second save", encoding="utf-8")
        second = inspect_session(session, fake_git)

        assert any(event.event_type == "FILE_CHANGED" for event in second)


def test_session_end_preserves_diff_and_event(app, tmp_path):
    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="b" * 40)
        db.session.add_all([project, session])
        db.session.commit()

        ended = end_session(session, FakeGit())

        assert ended.status == "COMPLETED"
        assert ended.ended_at is not None
        assert ended.file_changes[0].additions == 2
        assert Event.query.filter_by(session_id=session.id, event_type="SESSION_ENDED").count() == 1


def test_overlapping_end_requests_preserve_evidence_once(app, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    with app.app_context():
        project = Project(name="Game", path=str(tmp_path))
        session = Session(project=project, start_commit_hash="b" * 40)
        db.session.add(session)
        db.session.commit()
        session_id = session.id

    loaded = threading.Barrier(2)

    class CountingGit(FakeGit):
        calls = 0

        def session_diff(self, path, start_commit):
            self.calls += 1
            return super().session_diff(path, start_commit)

    git = CountingGit()

    def finish():
        with app.app_context():
            stale = db.session.get(Session, session_id)
            assert stale.status == "ACTIVE"
            loaded.wait(timeout=10)
            return end_session(stale, git).ended_at

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finish) for _ in range(2)]
        timestamps = [future.result(timeout=15) for future in futures]

    assert timestamps[0].replace(tzinfo=None) == timestamps[1].replace(tzinfo=None)
    assert git.calls == 1
    with app.app_context():
        completed = db.session.get(Session, session_id)
        assert len(completed.file_changes) == 1
        assert Event.query.filter_by(session_id=session_id, event_type="SESSION_ENDED").count() == 1
        assert inspect_session(completed, FakeGit([GitChange("late.cs", "CREATED", "?", "?")])) == []


def test_find_unity_project_inside_repository_subdirectory(tmp_path):
    nested = tmp_path / "course-work" / "MyPlatformer"
    for name in ("Assets", "Packages", "ProjectSettings"):
        (nested / name).mkdir(parents=True, exist_ok=True)
    ignored = tmp_path / "Library" / "OtherProject"
    for name in ("Assets", "Packages", "ProjectSettings"):
        (ignored / name).mkdir(parents=True, exist_ok=True)

    assert find_unity_projects(tmp_path) == [nested.resolve()]


def test_session_numbers_are_per_project(app, client, tmp_path):
    from types import SimpleNamespace
    from gamelearn.services.explanation_service import explanation_task_title, build_explanation_context
    from gamelearn.services.chat_service import chat_task_title

    first = Project(name="First", path=str(tmp_path / "first"))
    second = Project(name="Second", path=str(tmp_path / "second"))
    db.session.add_all([first, second])
    db.session.commit()
    one, _ = start_session(first, FakeGit())
    two, _ = start_session(second, FakeGit())
    end_session(one, FakeGit())
    three, _ = start_session(first, FakeGit())
    assert [one.number, two.number, three.number] == [1, 1, 2]
    assert [one.display_name, two.display_name, three.display_name] == [
        "Session 1", "Session 1", "Session 2"
    ]
    assert len({one.id, two.id, three.id}) == 3
    assert explanation_task_title(two).endswith("Second · Session 1")
    assert "Session 1 · Chat 7" in chat_task_title(two, SimpleNamespace(id=7))
    assert build_explanation_context(two)["current_session"]["number"] == 1
    page = client.get(f"/sessions/{two.id}").get_data(as_text=True)
    assert "Session 1" in page
    three.name = "Movement"
    db.session.commit()
    assert three.display_name == "Movement"
    assert three.number == 2
