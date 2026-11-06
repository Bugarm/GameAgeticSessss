import subprocess
from unittest.mock import patch

import pytest

from gamelearn.services.git_service import GitError, GitService, parse_porcelain_status


def test_parse_porcelain_status_handles_common_changes():
    output = " M Assets/Scripts/Player.cs\0?? Assets/Scripts/Enemy.cs\0 D Old.txt\0"

    changes = parse_porcelain_status(output)

    assert [(item.path, item.change_type) for item in changes] == [
        ("Assets/Scripts/Player.cs", "MODIFIED"),
        ("Assets/Scripts/Enemy.cs", "CREATED"),
        ("Old.txt", "DELETED"),
    ]


def test_parse_porcelain_status_handles_rename():
    changes = parse_porcelain_status("R  New.cs\0Old.cs\0")

    assert changes[0].change_type == "RENAMED"
    assert changes[0].path == "New.cs"
    assert changes[0].old_path == "Old.cs"


def test_repository_validation_uses_safe_subprocess(tmp_path):
    completed = subprocess.CompletedProcess([], 0, "true\n", "")
    with patch("subprocess.run", return_value=completed) as run:
        assert GitService().is_repository(tmp_path) is True

    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.args[0][0] == "git"
    assert "rev-parse" in run.call_args.args[0]


def test_missing_git_has_understandable_error(tmp_path):
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(GitError, match="not installed"):
            GitService().is_repository(tmp_path)


def test_session_diff_includes_tracked_and_untracked_text(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "GameLearn Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "Player.cs"
    tracked.write_text("class Player {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "Player.cs"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, capture_output=True, check=True)
    start = GitService().head_commit(tmp_path)
    tracked.write_text("class Player { int speed; }\n", encoding="utf-8")
    (tmp_path / "Enemy.cs").write_text("class Enemy {}\n", encoding="utf-8")

    records = GitService().session_diff(tmp_path, start)

    by_path = {record.path: record for record in records}
    assert by_path["Player.cs"].change_type == "MODIFIED"
    assert by_path["Enemy.cs"].change_type == "CREATED"
    assert "+class Enemy" in by_path["Enemy.cs"].diff_text


def test_git_operations_from_nested_unity_project_use_repository_root(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "GameLearn Test"], cwd=tmp_path, check=True)
    unity_project = tmp_path / "projects" / "Game"
    unity_project.mkdir(parents=True)
    tracked = unity_project / "Player.cs"
    tracked.write_text("class Player {}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, capture_output=True, check=True)
    start = GitService().head_commit(unity_project)
    tracked.write_text("class Player { int speed; }\n", encoding="utf-8")

    changes = GitService().status(unity_project)
    records = GitService().session_diff(unity_project, start)

    assert changes[0].path == "projects/Game/Player.cs"
    assert records[0].path == "projects/Game/Player.cs"


def test_code_diff_preserves_full_file_context_for_later_method_rendering(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "GameLearn Test"], cwd=tmp_path, check=True)
    code = tmp_path / "Player.cs"
    original = [f"// unchanged line {index}\n" for index in range(1, 31)]
    code.write_text("".join(original), encoding="utf-8")
    subprocess.run(["git", "add", "Player.cs"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, capture_output=True, check=True)
    start = GitService().head_commit(tmp_path)
    changed = list(original)
    changed[14] = "public void UpdatedMethod() {}\n"
    code.write_text("".join(changed), encoding="utf-8")

    record = GitService().session_diff(tmp_path, start)[0]

    assert "// unchanged line 1" in record.diff_text
    assert "// unchanged line 30" in record.diff_text
    assert "+public void UpdatedMethod() {}" in record.diff_text
