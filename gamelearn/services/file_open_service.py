"""Open only recorded session files with the operating system's configured application."""

import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

from gamelearn.services.git_service import GitService


TEXT_EDITOR_SUFFIXES = {
    ".asmdef", ".asmref", ".c", ".cginc", ".compute", ".cpp", ".cs", ".glsl",
    ".glslinc", ".h", ".hlsl", ".hpp", ".java", ".js", ".json", ".kt", ".log", ".md",
    ".py", ".shader", ".swift", ".ts", ".txt", ".uss", ".uxml", ".xml", ".yaml",
    ".yml",
}


class FileOpenError(RuntimeError):
    """A safe, user-facing failure while resolving or opening a recorded file."""


def resolve_session_file(session, relative_path, git_service=None):
    if not isinstance(relative_path, str) or not relative_path:
        raise FileOpenError("The selected code path is invalid.")
    allowed_paths = {change.path for change in session.file_changes}
    if relative_path not in allowed_paths:
        raise FileOpenError("That file was not recorded in this learning session.")

    posix_path = PurePosixPath(relative_path)
    if posix_path.is_absolute() or any(part in {"", ".", ".."} for part in posix_path.parts):
        raise FileOpenError("The selected code path is invalid.")

    git = git_service or GitService()
    repository_root = git.repository_root(session.project.path).resolve()
    try:
        target = (repository_root / Path(*posix_path.parts)).resolve(strict=True)
        target.relative_to(repository_root)
    except (OSError, ValueError) as exc:
        raise FileOpenError("That recorded file is no longer available.") from exc
    if not target.is_file():
        raise FileOpenError("The selected path is not an available code file.")
    if target.suffix.lower() not in TEXT_EDITOR_SUFFIXES:
        raise FileOpenError("Only text-based code files can be opened from a learning page.")
    return target


def open_session_file(session, relative_path, opener=None, git_service=None):
    target = resolve_session_file(session, relative_path, git_service=git_service)
    if opener:
        opener(target)
        return target
    if os.name == "nt":
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(
            ["open", str(target)],
            cwd=str(target.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    else:
        raise FileOpenError("Opening files is supported on Windows and macOS.")
    return target
