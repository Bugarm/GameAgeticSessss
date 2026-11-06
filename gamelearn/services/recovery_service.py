"""Import missed work without inventing observations or modifying Git."""
from dataclasses import asdict
from datetime import date
import hashlib
import json

from gamelearn import db
from gamelearn.models import Event, Session, SessionFileChange, utcnow
from gamelearn.services.git_service import GitService
from gamelearn.services.session_service import create_event, _evidence_lock


def recovery_preview(project, baseline, git_service=None):
    if project.is_archive:
        raise ValueError("Register a local project to recover a session.")
    if Session.query.filter_by(project_id=project.id, status="ACTIVE").first():
        raise ValueError("End the active session before recovering earlier work.")
    git = git_service or GitService()
    commits = git.recovery_commits(project.path)
    baseline = baseline or (commits[0]["hash"] if commits else "")
    if baseline not in {commit["hash"] for commit in commits}:
        raise ValueError("Choose one of the recent commits as the starting point.")
    head = git.head_commit(project.path)
    records = git.session_diff(project.path, baseline)
    fingerprint = hashlib.sha256(json.dumps(
        [project.id, baseline, head, [asdict(record) for record in records]],
        sort_keys=True).encode()).hexdigest()
    return commits, baseline, head, records, fingerprint


def recover_session(project, baseline, fingerprint, estimated_date="", git_service=None):
    with _evidence_lock:
        _, baseline, head, records, current = recovery_preview(project, baseline, git_service)
        if current != fingerprint:
            raise ValueError("The files or Git history changed. Preview the changes again before importing.")
        if not records:
            raise ValueError("There are no changes to recover from this commit.")
        previous_imports = Event.query.join(Session).filter(
            Session.project_id == project.id, Event.event_type == "SESSION_RECOVERED"
        ).all()
        if any(json.loads(event.metadata_json or "{}").get("fingerprint") == current
               for event in previous_imports):
            raise ValueError("These changes have already been imported as a recovered session.")
        if estimated_date:
            try:
                parsed = date.fromisoformat(estimated_date)
            except ValueError as exc:
                raise ValueError("Enter a valid estimated date.") from exc
            if parsed > date.today():
                raise ValueError("The estimated date cannot be in the future.")
            estimated_date = parsed.isoformat()
        now = utcnow()
        session = Session(project=project, status="COMPLETED", started_at=now, ended_at=now,
                          start_commit_hash=baseline, end_commit_hash=head)
        db.session.add(session)
        for record in records:
            db.session.add(SessionFileChange(session=session, **asdict(record)))
        create_event(session, "SYSTEM", "SESSION_RECOVERED", "Recovered session",
                     "Imported changes from a selected commit to the current files. "
                     "Edit times and intermediate steps were not observed.", "SUCCESS",
                     json.dumps({"estimated_date": estimated_date or None, "fingerprint": current}))
        db.session.commit()
        return session
