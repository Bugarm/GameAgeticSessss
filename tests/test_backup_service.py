import json
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from gamelearn import db, create_app
from gamelearn.models import Project, Session, Event, SessionFileChange, ChatConversation, ChatMessage, utcnow
from gamelearn.services.backup_service import BackupError, export_project, import_project, page_path, MAX_BACKUP_BYTES
from gamelearn.services.learning_page_service import build_learning_page_artifact
from test_learning_page_service import lesson_context, lesson_manifest


@pytest.fixture(autouse=True)
def diagram_validator(monkeypatch):
    validator = Mock()
    monkeypatch.setattr('gamelearn.services.backup_service.validate_learning_page_mermaid', validator)
    return validator


def seed():
    project = Project(name='Unity', path='/not-a-real-project')
    for name in ('Movement', 'Jumping'):
        session = Session(project=project, name=name, prompt="Add movement\nKeep jumping intact.", started_at=utcnow(), ended_at=utcnow(), status='COMPLETED', start_commit_hash='a' * 40, end_commit_hash='b' * 40)
        session.events.append(Event(source='GIT', event_type='CHANGE', title='Recorded change', metadata_json='{"path":"Assets/Player.cs"}'))
        session.file_changes.append(SessionFileChange(path='Assets/Player.cs', change_type='MODIFIED', additions=2, deletions=0, diff_text='@@ -1 +1 @@\n-old\n+new'))
        session.chat_conversations.append(ChatConversation(title='Why?', messages=[ChatMessage(role='student', content='Why?', status='COMPLETED'), ChatMessage(role='tutor', content='Because.', status='COMPLETED', codex_task_id='private-task')]))
        db.session.add(session)
    db.session.commit()
    return project


def test_import_backup_action_only_appears_on_home_page(app, client):
    project = seed()

    assert b'Import project backup' in client.get('/').data
    project_page = client.get(f'/projects/{project.id}')
    assert project_page.status_code == 200
    assert b'Import project backup' not in project_page.data
    assert b'Export project backup' in project_page.data


def test_project_round_trip_and_rename(app, client, diagram_validator):
    source = seed()
    for session in source.sessions:
        build_learning_page_artifact(json.dumps(lesson_manifest()), page_path(session.id), lesson_context(), Path(app.root_path) / 'templates')
    response = client.get(f'/projects/{source.id}/backup')
    assert response.status_code == 200
    assert 'attachment' in response.headers['Content-Disposition']
    assert b'private-task' not in response.data
    assert b'/not-a-real-project' not in response.data
    result = client.post('/projects/import', data={
        'csrf_token': app.config['CODEX_BRIDGE_CSRF_TOKEN'], 'backup': (BytesIO(response.data), 'backup.json'),
        'project_name': 'My restored project', 'names_reviewed': '1', 'session_name': ['Walking lesson', 'Jump lesson']})
    assert result.status_code == 302
    restored = Project.query.order_by(Project.id.desc()).first()
    assert restored.id != source.id
    assert restored.name == 'My restored project'
    assert restored.created_at == source.created_at
    assert restored.is_archive
    assert len(restored.sessions) == 2
    assert [s.name for s in restored.sessions] == ['Walking lesson', 'Jump lesson']
    assert [s.name for s in source.sessions] == ['Movement', 'Jumping']
    for original, copy in zip(source.sessions, restored.sessions):
        assert copy.prompt == original.prompt == "Add movement\nKeep jumping intact."
        assert copy.id != original.id
        assert copy.status == 'COMPLETED'
        assert copy.started_at == original.started_at
        assert copy.events[0].metadata_json == original.events[0].metadata_json
        assert copy.file_changes[0].diff_text == original.file_changes[0].diff_text
        assert copy.chat_conversations[0].messages[1].content == 'Because.'
        assert copy.chat_conversations[0].messages[1].codex_task_id is None
        assert page_path(copy.id).is_file()
        assert client.get(f'/sessions/{copy.id}/learning-page').status_code == 200
        assert copy.name.encode() in client.get(f'/sessions/{copy.id}/summary').data
    assert diagram_validator.call_count == 2
    assert b'Walking lesson' in client.get(result.headers['Location']).data
    assert b'Imported project history' in client.get('/').data
    assert b'archive:' not in client.get(result.headers['Location']).data
    exported = json.loads(export_project(restored))
    assert len(exported['sessions']) == 2
    assert exported['sessions'][0]['session']['name'] == 'Walking lesson'


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(version=2),
    lambda p: p['sessions'][1]['session'].update(status='ACTIVE'),
    lambda p: p['sessions'][1]['session'].update(started_at='invalid'),
    lambda p: p['sessions'][1].update(events={}),
    lambda p: p['sessions'][1]['changes'][0].update(is_binary='false'),
    lambda p: p['sessions'][1]['chats'][0]['messages'][0].update(role='system'),
    lambda p: p['sessions'][1].update(learning_page='<script>alert(1)</script>'),
    lambda p: p['sessions'][1].update(learning_page='<div onclick="alert(1)">Hello</div>'),
    lambda p: p['sessions'][1].update(learning_page='<a href="https://example.com">Hello</a>'),
    lambda p: p['sessions'][1]['events'][0].update(metadata_json='[]'),
    lambda p: p['project'].update(path='C:/arbitrary'),
    lambda p: p.update(sessions=None),
])
def test_invalid_later_session_leaves_no_partial_project(app, mutation):
    source = seed()
    payload = json.loads(export_project(source))
    mutation(payload)
    with pytest.raises(BackupError):
        import_project(json.dumps(payload).encode())
    assert Project.query.count() == 1
    assert Session.query.count() == 2


def test_duplicate_import_and_default_names(app):
    source = seed()
    raw = export_project(source)
    first = import_project(raw)
    second = import_project(raw, session_names=['', 'Renamed'])
    assert len({source.id, first.id, second.id}) == 3
    assert len({source.path, first.path, second.path}) == 3
    assert first.sessions[0].name == 'Movement'
    assert second.sessions[0].name is None
    assert second.sessions[0].display_name == 'Session 1'
    assert [session.number for session in first.sessions] == [1, 2]
    assert [session.number for session in second.sessions] == [1, 2]
    assert second.sessions[1].name == 'Renamed'
    assert not page_path(first.sessions[0].id).exists()


def test_export_requires_all_sessions_and_requests_finished(app):
    source = seed()
    source.sessions[1].status = 'ACTIVE'
    with pytest.raises(BackupError):
        export_project(source)
    source.sessions[1].status = 'COMPLETED'
    source.sessions[1].chat_conversations[0].messages[1].status = 'PENDING'
    with pytest.raises(BackupError):
        export_project(source)


def test_import_token_upload_size_and_form(app, client):
    seed()
    assert client.get('/projects/import').status_code == 200
    assert b'project-import.js' in client.get('/projects/import').data
    assert client.post('/projects/import').status_code == 403
    response = client.post('/projects/import', data={
        'csrf_token': app.config['CODEX_BRIDGE_CSRF_TOKEN'], 'backup': (BytesIO(b'not json'), 'bad.json')})
    assert response.status_code == 400
    with pytest.raises(BackupError):
        import_project(b' ' * (MAX_BACKUP_BYTES + 1))
    assert client.post('/projects/import', data=b'x' * (app.config['MAX_CONTENT_LENGTH'] + 1), content_type='multipart/form-data; boundary=test').status_code == 413
    assert Project.query.count() == 1


def test_all_artifacts_removed_on_database_failure(app, monkeypatch):
    source = seed()
    payload = json.loads(export_project(source))
    for entry in payload['sessions']:
        entry['learning_page'] = '<p>Saved lesson</p>'
    def fail():
        raise SQLAlchemyError('failed')
    monkeypatch.setattr(db.session, 'commit', fail)
    with pytest.raises(SQLAlchemyError):
        import_project(json.dumps(payload).encode())
    assert Project.query.count() == 1
    assert Session.query.count() == 2
    assert not list(page_path(1).parent.glob('*.html'))


def test_second_diagram_failure_removes_first_artifact(app, diagram_validator):
    from gamelearn.services.codex_bridge import CodexBridgeError
    source = seed()
    payload = json.loads(export_project(source))
    for entry in payload['sessions']:
        entry['learning_page'] = '<pre class="mermaid">invalid</pre>'
    diagram_validator.side_effect = [None, CodexBridgeError('invalid')]
    with pytest.raises(BackupError):
        import_project(json.dumps(payload).encode())
    assert Project.query.count() == 1
    assert Session.query.count() == 2
    assert not list(page_path(1).parent.glob('*.html'))


def test_existing_orphan_artifact_is_preserved(app):
    source = seed()
    payload = json.loads(export_project(source))
    payload['sessions'][0]['learning_page'] = '<p>Lesson</p>'
    orphan = page_path(3)
    orphan.parent.mkdir(parents=True)
    orphan.write_text('Existing content')
    with pytest.raises(FileExistsError):
        import_project(json.dumps(payload).encode())
    assert orphan.read_text() == 'Existing content'
    assert Project.query.count() == 1


@pytest.mark.parametrize('options', [{'session_names': ['Only one']}, {'session_names': ['x'*201, 'ok']}, {'project_name': ''}, {'project_name': 'x'*201}])
def test_invalid_rename_rejected(app, options):
    source = seed()
    with pytest.raises(BackupError):
        import_project(export_project(source), **options)
    assert Project.query.count() == 1


def test_archive_does_not_access_git_or_monitor(app, client, monkeypatch):
    archive = import_project(export_project(seed()))
    def forbidden(*args, **kwargs):
        raise AssertionError('Archive must not inspect a repository')
    monkeypatch.setattr('gamelearn.routes.validate_unity_project', forbidden)
    monkeypatch.setattr('gamelearn.routes.start_session', forbidden)
    assert client.get(f'/projects/{archive.id}').status_code == 200
    assert client.post(f'/projects/{archive.id}/sessions').status_code == 302
    assert Session.query.filter_by(status='ACTIVE').count() == 0


def test_existing_database_gets_new_session_fields_without_losing_data(tmp_path):
    import sqlite3

    database = tmp_path / 'old.db'
    with sqlite3.connect(database) as connection:
        connection.executescript('''
        CREATE TABLE project (
            id INTEGER PRIMARY KEY,
            name VARCHAR(200) NOT NULL,
            path TEXT NOT NULL UNIQUE,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE session (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            started_at DATETIME NOT NULL,
            ended_at DATETIME,
            status VARCHAR(30) NOT NULL,
            start_commit_hash VARCHAR(64) NOT NULL,
            end_commit_hash VARCHAR(64)
        );
        INSERT INTO project VALUES (1, 'Original', '/original', '2026-01-01 00:00:00');
        INSERT INTO session VALUES (
            1, 1, '2026-01-01 00:00:00', NULL, 'ACTIVE', 'abc', NULL
        );
        ''')

    config = {
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{database}',
        'START_SESSION_MONITOR': False,
    }
    for _ in range(2):
        upgraded_app = create_app(config)
        with upgraded_app.app_context():
            project = db.session.get(Project, 1)
            session = db.session.get(Session, 1)
            assert project.name == 'Original'
            assert project.is_archive is False
            assert session.start_commit_hash == 'abc'
            assert session.name is None
            assert session.prompt is None
            assert session.notes is None
