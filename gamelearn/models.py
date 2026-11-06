import json
from datetime import datetime, timezone

from gamelearn import db


def utcnow():
    return datetime.now(timezone.utc)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    path = db.Column(db.Text, nullable=False, unique=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    is_archive = db.Column(db.Boolean, default=False, nullable=False)
    sessions = db.relationship("Session", backref="project", lazy=True, cascade="all, delete-orphan")


class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    name = db.Column(db.String(200), nullable=True)
    prompt = db.Column(db.Text(20000), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    @property
    def notes_delta(self):
        return json.loads(self.notes) if self.notes else {"ops": [{"insert": "\n"}]}

    started_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(30), default="ACTIVE", nullable=False)
    start_commit_hash = db.Column(db.String(64), nullable=False)
    end_commit_hash = db.Column(db.String(64), nullable=True)
    events = db.relationship("Event", backref="session", lazy=True, cascade="all, delete-orphan")
    file_changes = db.relationship(
        "SessionFileChange", backref="session", lazy=True, cascade="all, delete-orphan"
    )
    chat_conversations = db.relationship(
        "ChatConversation", backref="session", lazy=True, cascade="all, delete-orphan"
    )

    @property
    def recovery(self):
        for event in self.events:
            if event.event_type == "SESSION_RECOVERED":
                try:
                    metadata = json.loads(event.metadata_json or "{}")
                except (ValueError, TypeError):
                    metadata = {}
                return {"estimated_date": metadata.get("estimated_date") if isinstance(metadata, dict) else None}
        return None

    @property
    def number(self):
        """One-based position among this project's sessions, in creation order."""
        return Session.query.filter(
            Session.project_id == self.project_id, Session.id <= self.id
        ).count()

    @property
    def display_name(self):
        return self.name or f"Session {self.number}"


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("session.id"), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    source = db.Column(db.String(30), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default="INFO", nullable=False)
    metadata_json = db.Column(db.Text, nullable=True)


    @property
    def changed_files(self):
        """Expose paths from persisted Git snapshots, including older events."""
        if self.event_type != "GIT_STATUS" or not self.metadata_json:
            return []
        try:
            metadata = json.loads(self.metadata_json)
        except (TypeError, ValueError):
            return []
        snapshot = metadata.get("snapshot", {}) if isinstance(metadata, dict) else {}
        if not isinstance(snapshot, dict):
            return []
        files = []
        for key in snapshot:
            parts = key.split(":", 2)
            if len(parts) != 3:
                continue
            change_type, old_path, path = parts
            files.append({"change_type": change_type.title(), "path": path, "old_path": old_path})
        return files


class SessionFileChange(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("session.id"), nullable=False)
    path = db.Column(db.Text, nullable=False)
    change_type = db.Column(db.String(30), nullable=False)
    additions = db.Column(db.Integer, nullable=True)
    deletions = db.Column(db.Integer, nullable=True)
    diff_text = db.Column(db.Text, nullable=True)
    is_binary = db.Column(db.Boolean, default=False, nullable=False)


class ChatConversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("session.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    messages = db.relationship(
        "ChatMessage", backref="conversation", lazy=True, cascade="all, delete-orphan"
    )


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("chat_conversation.id"), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="COMPLETED", nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    codex_task_id = db.Column(db.String(255), nullable=True)
