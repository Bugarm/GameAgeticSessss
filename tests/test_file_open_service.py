from types import SimpleNamespace

import pytest

from gamelearn.services.file_open_service import FileOpenError, open_session_file


class FakeGit:
    def __init__(self, root):
        self.root = root

    def repository_root(self, project_path):
        return self.root


def _session(root, *paths):
    return SimpleNamespace(
        project=SimpleNamespace(path=str(root / "NestedUnity")),
        file_changes=[SimpleNamespace(path=path) for path in paths],
    )


def test_open_session_file_resolves_recorded_path_from_git_root(tmp_path):
    target = tmp_path / "Assets" / "Player.cs"
    target.parent.mkdir()
    target.write_text("class Player {}", encoding="utf-8")
    opened = []

    result = open_session_file(
        _session(tmp_path, "Assets/Player.cs"),
        "Assets/Player.cs",
        opener=opened.append,
        git_service=FakeGit(tmp_path),
    )

    assert result == target.resolve()
    assert opened == [target.resolve()]


def test_open_session_file_rejects_unrecorded_or_traversal_path(tmp_path):
    session = _session(tmp_path, "Assets/Player.cs", "../outside.cs")

    with pytest.raises(FileOpenError, match="not recorded"):
        open_session_file(session, "Assets/Other.cs", opener=lambda path: None, git_service=FakeGit(tmp_path))
    with pytest.raises(FileOpenError, match="invalid"):
        open_session_file(session, "../outside.cs", opener=lambda path: None, git_service=FakeGit(tmp_path))


def test_open_session_file_rejects_unity_scene_even_when_recorded(tmp_path):
    target = tmp_path / "Assets" / "BeachCourt.unity"
    target.parent.mkdir()
    target.write_text("%YAML 1.1", encoding="utf-8")

    with pytest.raises(FileOpenError, match="Only text-based code files"):
        open_session_file(
            _session(tmp_path, "Assets/BeachCourt.unity"),
            "Assets/BeachCourt.unity",
            opener=lambda path: None,
            git_service=FakeGit(tmp_path),
        )


def test_open_session_file_allows_recorded_log_file(tmp_path):
    target = tmp_path / "Logs" / "session.log"
    target.parent.mkdir()
    target.write_text("Unity log", encoding="utf-8")
    opened = []

    result = open_session_file(
        _session(tmp_path, "Logs/session.log"),
        "Logs/session.log",
        opener=opened.append,
        git_service=FakeGit(tmp_path),
    )

    assert result == target.resolve()
    assert opened == [target.resolve()]
