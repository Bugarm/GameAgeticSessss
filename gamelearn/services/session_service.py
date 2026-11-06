from pathlib import Path
from functools import wraps
import json
import threading
import time

from gamelearn import db
from gamelearn.models import Event, Session, SessionFileChange, utcnow
from gamelearn.services.git_service import GitService


UNITY_MARKERS = ("Assets", "Packages", "ProjectSettings")
_evidence_lock = threading.RLock()


def _serialize_evidence(operation):
    """Serialize monitor and request writes in the local single-process app."""
    @wraps(operation)
    def guarded(session, *args, **kwargs):
        with _evidence_lock:
            # A waiting request may have loaded ACTIVE before another finished.
            db.session.refresh(session)
            return operation(session, *args, **kwargs)
    return guarded


UNITY_SCAN_EXCLUSIONS = {
    ".git",
    ".tools",
    ".venv",
    "Library",
    "Logs",
    "Temp",
    "UserSettings",
    "node_modules",
    "obj",
}


def validate_unity_project(path):
    project_path = Path(path)
    missing = [name for name in UNITY_MARKERS if not (project_path / name).is_dir()]
    return not missing, missing


def find_unity_projects(path, max_depth=5):
    """Find Unity project roots below a selected Git repository directory."""
    root = Path(path).resolve()
    detected, _ = validate_unity_project(root)
    if detected:
        return [root]

    candidates = []
    pending = [(root, 0)]
    while pending:
        current, depth = pending.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = [item for item in current.iterdir() if item.is_dir()]
        except OSError:
            continue
        for child in children:
            if child.name in UNITY_SCAN_EXCLUSIONS:
                continue
            is_unity, _ = validate_unity_project(child)
            if is_unity:
                candidates.append(child.resolve())
            else:
                pending.append((child, depth + 1))
    return sorted(candidates, key=lambda item: str(item).lower())


def create_event(session, source, event_type, title, description=None, status="INFO", metadata_json=None):
    event = Event(
        session=session,
        source=source,
        event_type=event_type,
        title=title,
        description=description,
        status=status,
        metadata_json=metadata_json,
    )
    db.session.add(event)
    return event


def start_session(project, git_service=None):
    git = git_service or GitService()
    active = Session.query.filter_by(project_id=project.id, status="ACTIVE").first()
    if active:
        raise ValueError("This project already has an active GameLearn session.")
    git.require_repository(project.path)
    changes = git.status(project.path)
    if changes:
        return None, changes

    session = Session(project=project, start_commit_hash=git.head_commit(project.path), status="ACTIVE")
    db.session.add(session)
    create_event(
        session,
        "SYSTEM",
        "SESSION_STARTED",
        "Session started",
        "GameLearn recorded a clean Git baseline.",
        "SUCCESS",
    )
    db.session.commit()
    return session, []


@_serialize_evidence
def inspect_session(session, git_service=None):
    """Record project evidence only when the observed Git/file state changes."""
    if session.status != "ACTIVE":
        return []
    git = git_service or GitService()
    changes = git.status(session.project.path)
    repository_root = git.repository_root(session.project.path)
    snapshot = {_change_key(change): _file_signature(repository_root, change) for change in changes}
    previous = _last_snapshot(session.id)
    if snapshot == previous:
        return []

    recorded = []
    for change in changes:
        key = _change_key(change)
        if previous.get(key) == snapshot[key]:
            continue
        event_type = {
            "CREATED": "FILE_CREATED",
            "DELETED": "FILE_DELETED",
            "RENAMED": "FILE_CHANGED",
        }.get(change.change_type, "FILE_CHANGED")
        description = change.path
        if change.old_path:
            description = f"{change.old_path} → {change.path}"
        recorded.append(
            create_event(
                session,
                "GIT",
                event_type,
                _event_title(change.change_type),
                description,
                "INFO",
                json.dumps({"path": change.path, "signature": snapshot[key]}),
            )
        )

    status = "INFO" if changes else "SUCCESS"
    status_event = create_event(
        session,
        "GIT",
        "GIT_STATUS",
        "Changes detected" if changes else "Working tree clean",
        f"{len(changes)} changed file(s) observed." if changes else "No uncommitted changes detected.",
        status,
        json.dumps({"snapshot": snapshot}, sort_keys=True),
    )
    recorded.append(status_event)
    db.session.commit()
    return recorded


def _last_snapshot(session_id):
    event = (
        Event.query.filter_by(session_id=session_id, event_type="GIT_STATUS")
        .order_by(Event.id.desc())
        .first()
    )
    if not event or not event.metadata_json:
        return {}
    try:
        value = json.loads(event.metadata_json).get("snapshot", {})
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _change_key(change):
    return f"{change.change_type}:{change.old_path or ''}:{change.path}"


def _file_signature(project_path, change):
    target = Path(project_path) / change.path
    if change.change_type == "DELETED":
        return "deleted"
    try:
        stat = target.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return "unavailable"


def _event_title(change_type):
    return {
        "CREATED": "File created",
        "DELETED": "File deleted",
        "RENAMED": "File renamed",
    }.get(change_type, "Code changed")


class SessionMonitor:
    """Small in-process monitor for the single-user local prototype."""

    def __init__(self, app):
        self.app = app
        self.interval = app.config.get("GIT_POLL_INTERVAL_SECONDS", 3)
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="gamelearn-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            with self.app.app_context():
                sessions = Session.query.filter_by(status="ACTIVE").all()
                for session in sessions:
                    try:
                        inspect_session(session)
                    except Exception as exc:  # The monitor must survive unavailable projects/Git.
                        db.session.rollback()
                        self.app.logger.warning("Session %s polling failed: %s", session.id, exc)
            self._stop_event.wait(self.interval)


@_serialize_evidence
def end_session(session, git_service=None):
    if session.status == "COMPLETED":
        return session
    if session.status != "ACTIVE":
        raise ValueError("This GameLearn session has already ended.")
    git = git_service or GitService()
    inspect_session(session, git)
    records = git.session_diff(session.project.path, session.start_commit_hash)
    for existing in list(session.file_changes):
        db.session.delete(existing)
    for record in records:
        db.session.add(
            SessionFileChange(
                session=session,
                path=record.path,
                change_type=record.change_type,
                additions=record.additions,
                deletions=record.deletions,
                diff_text=record.diff_text,
                is_binary=record.is_binary,
            )
        )
    session.end_commit_hash = git.head_commit(session.project.path)
    session.ended_at = utcnow()
    session.status = "COMPLETED"
    create_event(
        session,
        "SYSTEM",
        "SESSION_ENDED",
        "Session ended",
        f"GameLearn preserved evidence for {len(records)} changed file(s).",
        "SUCCESS",
    )
    db.session.commit()
    return session
