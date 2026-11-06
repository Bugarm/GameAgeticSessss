"""Remove local GameLearn history without accessing a registered repository."""
from uuid import uuid4

from flask import current_app

from gamelearn import db
from gamelearn.services.backup_service import page_path


def remove_project_history(project):
    sessions = list(project.sessions)
    dispatcher = current_app.extensions["codex_explanations"]
    for session in sessions:
        if session.status == "ACTIVE":
            raise ValueError("End active sessions before removing this project.")
        job = dispatcher.status(session.id)
        if job and job.status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError("Finish or stop learning-page requests before removing this project.")
        if any(message.status == "PENDING" for chat in session.chat_conversations for message in chat.messages):
            raise ValueError("Wait for tutor answers to finish before removing this project.")

    # Stage only known generated pages. Restore them if the database transaction fails.
    staged = []
    try:
        for session in sessions:
            path = page_path(session.id)
            if path.exists() or path.is_symlink():
                temporary = path.with_name(f".removed-{uuid4().hex}.html")
                path.rename(temporary)
                staged.append((path, temporary))
        db.session.delete(project)
        db.session.commit()
    except Exception:
        db.session.rollback()
        for path, temporary in reversed(staged):
            temporary.rename(path)
        raise

    for session in sessions:
        dispatcher.forget(session.id)
        for chat in session.chat_conversations:
            for message in chat.messages:
                current_app.extensions["codex_chat"].forget(message.id)
    for _, temporary in staged:
        try:
            temporary.unlink()
        except OSError:
            current_app.logger.warning("A removed learning-page temporary file could not be cleaned up.")
