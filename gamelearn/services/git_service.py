import os
import subprocess
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from gamelearn.services.code_files import CODE_FILE_EXTENSIONS


class GitError(RuntimeError):
    """A student-facing Git operation failure."""


@dataclass(frozen=True)
class GitChange:
    path: str
    change_type: str
    index_status: str
    worktree_status: str
    old_path: str | None = None


@dataclass(frozen=True)
class GitDiffRecord:
    path: str
    change_type: str
    additions: int | None
    deletions: int | None
    diff_text: str | None
    is_binary: bool = False


class GitService:
    def __init__(self, timeout_seconds=30):
        self.timeout_seconds = timeout_seconds

    def _run(self, project_path, *arguments):
        path = Path(project_path)
        if not path.is_dir():
            raise GitError("The project directory is unavailable.")
        command = ["git"]
        repository_hint = _repository_hint(path)
        if repository_hint:
            command.extend(["-c", f"safe.directory={repository_hint}"])
        command.extend(arguments)
        try:
            return subprocess.run(
                command,
                cwd=str(path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise GitError("Git is not installed or is not available on PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError("Git did not respond before the timeout.") from exc
        except OSError as exc:
            raise GitError(f"Git could not be started: {exc}.") from exc

    def is_repository(self, project_path):
        result = self._run(project_path, "rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def require_repository(self, project_path):
        if not self.is_repository(project_path):
            raise GitError("This Unity project is not a Git repository.")

    def repository_root(self, project_path):
        result = self._run(project_path, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            raise GitError("Git could not locate the repository root.")
        return Path(result.stdout.strip()).resolve()

    def head_commit(self, project_path):
        result = self._run(self.repository_root(project_path), "rev-parse", "HEAD")
        if result.returncode != 0:
            raise GitError("Git could not read HEAD. Make sure the repository has at least one commit.")
        return result.stdout.strip()

    def file_at_commit(self, project_path, commit, relative_path):
        """Read one repository file at a recorded commit without touching the working tree."""
        repository_root = self.repository_root(project_path)
        result = self._run(repository_root, "show", f"{commit}:{relative_path}")
        if result.returncode != 0:
            return None
        return result.stdout

    def status(self, project_path):
        self.require_repository(project_path)
        result = self._run(
            self.repository_root(project_path),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "Git could not inspect the working tree.")
        return parse_porcelain_status(result.stdout)

    def is_clean(self, project_path):
        return not self.status(project_path)

    def recovery_commits(self, project_path):
        """Offer a bounded list of committed baselines reachable from HEAD."""
        result = self._run(self.repository_root(project_path), "log", "-100",
                           "--format=%H%x09%cs%x09%s", "HEAD", "--")
        if result.returncode != 0:
            raise GitError("Git could not read commits. The repository needs at least one commit.")
        return [dict(zip(("hash", "date", "subject"), line.split("\t", 2)))
                for line in result.stdout.splitlines() if line.count("\t") >= 2]

    def session_diff(self, project_path, start_commit, max_text_bytes=500_000):
        self.require_repository(project_path)
        repository_root = self.repository_root(project_path)
        name_result = self._run(repository_root, "diff", "--name-status", "-M", start_commit, "--")
        if name_result.returncode != 0:
            raise GitError(name_result.stderr.strip() or "Git could not calculate the session diff.")
        entries = _parse_name_status(name_result.stdout)
        known_paths = {entry[1] for entry in entries}
        status = self.status(project_path)
        for change in status:
            if change.change_type == "CREATED" and change.path not in known_paths:
                entries.append(("CREATED", change.path))

        records = []
        for change_type, path in entries:
            if change_type == "CREATED" and _is_untracked(status, path):
                records.append(_untracked_diff(repository_root, path, max_text_bytes))
                continue
            numstat = self._run(repository_root, "diff", "--numstat", start_commit, "--", path)
            additions, deletions, is_binary = _parse_numstat(numstat.stdout)
            diff_text = None
            if not is_binary:
                diff = self._run(
                    repository_root,
                    "diff",
                    "--no-ext-diff",
                    (
                        "--unified=1000000"
                        if Path(path).suffix.lower() in CODE_FILE_EXTENSIONS
                        else "--unified=3"
                    ),
                    start_commit,
                    "--",
                    path,
                )
                diff_text = diff.stdout[:max_text_bytes]
                if len(diff.stdout) > max_text_bytes:
                    diff_text += "\n... diff truncated by GameLearn ...\n"
            records.append(GitDiffRecord(path, change_type, additions, deletions, diff_text, is_binary))
        return records


def parse_porcelain_status(output):
    if not output:
        return []
    entries = output.split("\0")
    changes = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            continue
        index_status, worktree_status = entry[0], entry[1]
        path = entry[3:]
        old_path = None
        if index_status in "RC" or worktree_status in "RC":
            if index < len(entries):
                old_path = entries[index] or None
                index += 1
        changes.append(
            GitChange(
                path=_display_path(path),
                change_type=_change_type(index_status, worktree_status),
                index_status=index_status,
                worktree_status=worktree_status,
                old_path=_display_path(old_path) if old_path else None,
            )
        )
    return changes


def _change_type(index_status, worktree_status):
    states = {index_status, worktree_status}
    if "?" in states or "A" in states:
        return "CREATED"
    if "D" in states:
        return "DELETED"
    if "R" in states:
        return "RENAMED"
    return "MODIFIED"


def _display_path(path):
    return path.replace(os.sep, "/").replace("\\", "/")


def _repository_hint(path):
    current = Path(path).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def _parse_name_status(output):
    records = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        code = fields[0]
        if code.startswith("R") and len(fields) >= 3:
            records.append(("RENAMED", _display_path(fields[2])))
        else:
            change_type = {"A": "CREATED", "D": "DELETED"}.get(code[:1], "MODIFIED")
            records.append((change_type, _display_path(fields[1])))
    return records


def _parse_numstat(output):
    line = next((item for item in output.splitlines() if item.strip()), "")
    fields = line.split("\t")
    if len(fields) < 2:
        return 0, 0, False
    if fields[0] == "-" or fields[1] == "-":
        return None, None, True
    try:
        return int(fields[0]), int(fields[1]), False
    except ValueError:
        return 0, 0, False


def _is_untracked(changes, path):
    return any(change.path == path and change.index_status == "?" for change in changes)


def _untracked_diff(project_path, relative_path, max_text_bytes):
    target = project_path / relative_path
    try:
        content = target.read_bytes()
    except OSError:
        return GitDiffRecord(relative_path, "CREATED", 0, 0, None, False)
    if b"\0" in content:
        return GitDiffRecord(relative_path, "CREATED", None, None, None, True)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return GitDiffRecord(relative_path, "CREATED", None, None, None, True)
    lines = text.splitlines(keepends=True)
    diff = "".join(unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{relative_path}"))
    if len(diff.encode("utf-8")) > max_text_bytes:
        diff = diff[:max_text_bytes] + "\n... diff truncated by GameLearn ...\n"
    return GitDiffRecord(relative_path, "CREATED", len(lines), 0, diff, False)
