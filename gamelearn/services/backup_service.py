"""Bounded portable project backups; never accesses the Unity repository."""
import json
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4
from flask import current_app
from gamelearn import db
from gamelearn.models import Project, Session, Event, SessionFileChange, ChatConversation, ChatMessage
from gamelearn.services.codex_bridge import CodexBridgeError, validate_learning_page_mermaid

MAX_BACKUP_BYTES = 20 * 1024 * 1024
FIELDS = {
    Session: 'name prompt notes started_at ended_at status start_commit_hash end_commit_hash',
    Event: 'timestamp source event_type title description status metadata_json',
    SessionFileChange: 'path change_type additions deletions diff_text is_binary',
    ChatConversation: 'title created_at updated_at',
    ChatMessage: 'role content status created_at',
}

class BackupError(ValueError):
    pass

class SafePage(HTMLParser):
    """Reconstruct only inert markup from the fixed lesson renderer."""
    tags = set('a article aside button code div fieldset form h1 h2 h3 header input label legend li nav ol p pre section span strong textarea ul em br'.split())
    attrs = set('class id for name type value hidden disabled checked rows placeholder'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            raise BackupError('The backup learning page contains unsupported markup.')
        clean = []
        for key, value in attrs:
            if key in self.attrs or key.startswith('aria-') or key.startswith('data-gamelearn-') or (key == 'href' and (value or '').startswith('#')):
                clean.append(f' {key}="{escape(value or "", quote=True)}"')
            else:
                raise BackupError('The backup learning page contains unsupported attributes.')
        self.parts.append('<' + tag + ''.join(clean) + '>')

    def handle_endtag(self, tag):
        if tag not in self.tags:
            raise BackupError('The backup learning page contains unsupported markup.')
        self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        self.parts.append(escape(data))


def page_path(session_id):
    return Path(current_app.config['LEARNING_PAGE_ROOT']) / f'gamelearn-session-{session_id}.html'


def record(obj):
    return {key: (value.isoformat() if isinstance(value, datetime) else value)
            for key in FIELDS[type(obj)].split() for value in [getattr(obj, key)]}


def session_payload(session):
    if session.status != 'COMPLETED':
        raise BackupError('End the session before exporting a backup.')
    if any(m.status == 'PENDING' for c in session.chat_conversations for m in c.messages):
        raise BackupError('Wait for tutor answers to finish before exporting.')
    job = current_app.extensions['codex_explanations'].status(session.id)
    if job and job.payload().get('status') not in {'COMPLETED', 'FAILED', 'CANCELLED'}:
        raise BackupError('Wait for the learning page to finish before exporting.')
    path = page_path(session.id)
    payload = dict(format='gamelearn-session', version=1, session=record(session),
                   events=[record(x) for x in sorted(session.events, key=lambda x: x.id)],
                   changes=[record(x) for x in session.file_changes],
                   chats=[dict(record(c), messages=[record(m) for m in sorted(c.messages, key=lambda x: x.id)])
                          for c in session.chat_conversations],
                   learning_page=path.read_text(encoding='utf-8') if path.is_file() else None)
    return payload


def validate_record(model, value):
    if not isinstance(value, dict) or set(value) != set(FIELDS[model].split()):
        raise BackupError('The backup contains an invalid record.')
    result = dict(value)
    for key, item in result.items():
        column = model.__table__.columns[key]
        if item is None:
            if not column.nullable:
                raise BackupError('A required backup field is missing.')
            continue
        expected = column.type.python_type
        if expected is datetime:
            try:
                result[key] = datetime.fromisoformat(item)
            except (TypeError, ValueError):
                raise BackupError('The backup contains an invalid date.') from None
        elif type(item) is not expected:
            raise BackupError('The backup contains an invalid field type.')
        elif isinstance(item, str) and column.type.length and len(item) > column.type.length:
            raise BackupError('A backup field is too long.')
    return result


def build_session(payload):
    if not isinstance(payload, dict) or set(payload) != {'format', 'version', 'session', 'events', 'changes', 'chats', 'learning_page'}:
        raise BackupError('The backup has invalid session fields.')
    if payload['format'] != 'gamelearn-session' or type(payload['version']) is not int or payload['version'] != 1:
        raise BackupError('The backup session format is not supported.')
    data = validate_record(Session, payload['session'])
    from gamelearn.services.session_notes import validate_notes
    try:
        data['notes'] = validate_notes(data['notes'])
    except ValueError as exc:
        raise BackupError(str(exc)) from None
    if data['status'] != 'COMPLETED' or data['ended_at'] is None:
        raise BackupError('Only completed sessions can be restored.')
    for key in ('events', 'changes', 'chats'):
        if not isinstance(payload[key], list) or len(payload[key]) > 50000:
            raise BackupError('The backup contains an invalid collection.')
    session = Session(**data)
    session.events = [Event(**validate_record(Event, x)) for x in payload['events']]
    for event in session.events:
        if event.metadata_json:
            try:
                metadata = json.loads(event.metadata_json)
            except (ValueError, RecursionError):
                raise BackupError('The backup contains invalid event metadata.') from None
            if not isinstance(metadata, dict):
                raise BackupError('The backup contains invalid event metadata.')
    session.file_changes = [SessionFileChange(**validate_record(SessionFileChange, x)) for x in payload['changes']]
    for chat in payload['chats']:
        if not isinstance(chat, dict) or not isinstance(chat.get('messages'), list) or len(chat['messages']) > 50000:
            raise BackupError('The backup contains an invalid chat.')
        conversation = ChatConversation(**validate_record(ChatConversation, {k: v for k, v in chat.items() if k != 'messages'}))
        for message in chat['messages']:
            fields = validate_record(ChatMessage, message)
            if fields['role'] not in {'student', 'tutor'} or fields['status'] not in {'COMPLETED', 'FAILED', 'CANCELLED'}:
                raise BackupError('The backup contains an invalid message state.')
            conversation.messages.append(ChatMessage(**fields))
        session.chat_conversations.append(conversation)
    page = payload['learning_page']
    if page is not None:
        if not isinstance(page, str) or len(page.encode('utf-8')) > 1_000_000:
            raise BackupError('The backup learning page is invalid or too large.')
        parser = SafePage()
        parser.feed(page)
        parser.close()
        page = ''.join(parser.parts)
    return session, page


def export_project(project):
    payload = dict(format='gamelearn-project', version=1,
                   project=dict(name=project.name, created_at=project.created_at.isoformat()),
                   sessions=[session_payload(session) for session in sorted(project.sessions, key=lambda x: (x.started_at, x.id))])
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(raw) > MAX_BACKUP_BYTES:
        raise BackupError('This project exceeds the 20 MB backup limit.')
    return raw


def import_project(raw, project_name=None, session_names=None):
    if len(raw) > MAX_BACKUP_BYTES:
        raise BackupError('Backups must be smaller than 20 MB.')
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise BackupError('Choose a valid GameLearn project JSON backup.') from None
    if not isinstance(payload, dict) or set(payload) != {'format', 'version', 'project', 'sessions'}:
        raise BackupError('Choose a whole-project GameLearn backup.')
    if payload['format'] != 'gamelearn-project' or type(payload['version']) is not int or payload['version'] != 1:
        raise BackupError('This backup format or version is not supported.')
    source = payload['project']
    if not isinstance(source, dict) or set(source) != {'name', 'created_at'}:
        raise BackupError('The backup project details are invalid.')
    name = clean_name(project_name if project_name is not None else source['name'])
    if not name:
        raise BackupError('Enter a project name.')
    try:
        created_at = datetime.fromisoformat(source['created_at'])
    except (ValueError, TypeError):
        raise BackupError('The backup project date is invalid.') from None
    entries = payload['sessions']
    if not isinstance(entries, list) or len(entries) > 10000:
        raise BackupError('The backup session list is invalid.')
    if session_names is not None and (not isinstance(session_names, list) or len(session_names) != len(entries)):
        raise BackupError('Provide one name field for each imported session.')
    prepared = [build_session(entry) for entry in entries]
    for index, (session, _) in enumerate(prepared):
        session.name = clean_name(session_names[index]) if session_names is not None else clean_name(session.name)
    # Archive identity is generated locally; no imported path becomes a live project.
    project = Project(name=name, created_at=created_at, path=f'archive:{uuid4().hex}', is_archive=True)
    project.sessions = [session for session, _ in prepared]
    created_paths = []
    try:
        db.session.add(project)
        db.session.flush()
        for session, page in prepared:
            if not page:
                continue
            destination = page_path(session.id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('x', encoding='utf-8', newline='\n') as output:
                created_paths.append(destination)
                output.write(page)
            try:
                validate_learning_page_mermaid(destination, Path(current_app.root_path).parent)
            except CodexBridgeError:
                raise BackupError('The backup learning page failed diagram validation.') from None
        db.session.commit()
    except Exception:
        db.session.rollback()
        for destination in created_paths:
            destination.unlink(missing_ok=True)
        raise
    return project


def clean_name(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 200:
        raise BackupError('Names must be text of at most 200 characters.')
    return value.strip() or None
