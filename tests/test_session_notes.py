import json
import base64

import pytest

from gamelearn import db
from gamelearn.models import Project, Session, utcnow
from gamelearn.services.backup_service import BackupError, export_project, import_project
from gamelearn.services.explanation_service import build_explanation_context
from gamelearn.services.session_notes import validate_notes


PICTURE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='


@pytest.mark.parametrize('embed', [{'image': PICTURE}, {'video': 'https://www.youtube-nocookie.com/embed/QHH3iSeDBLo'}])
def test_embed_notes_database_and_backup_roundtrip(app, client, embed):
    session = Session(project=Project(name='Pictures', path='archive:pictures', is_archive=True),
                      start_commit_hash='a' * 40, status='COMPLETED', ended_at=utcnow())
    db.session.add(session)
    db.session.commit()
    delta = {'ops': [{'insert': embed}, {'insert': '\n'}]}
    response = client.post(f'/api/sessions/{session.id}/notes',
                           json={'notes': json.dumps(delta)},
                           headers={'X-GameLearn-Token': app.config['CODEX_BRIDGE_CSRF_TOKEN']})
    assert response.status_code == 200
    db.session.expire_all()
    assert session.notes_delta == delta
    raw = export_project(session.project)
    assert next(iter(embed.values())).encode() in raw
    restored = import_project(raw)
    assert restored.sessions[0].notes_delta == delta
    assert 'notes' not in build_explanation_context(session)['current_session']


def test_video_only_notes_are_preserved():
    delta = {'ops': [{'insert': {'video': 'https://www.youtube-nocookie.com/embed/QHH3iSeDBLo'}}]}
    assert json.loads(validate_notes(json.dumps(delta))) == delta


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/embed/QHH3iSeDBLo',
    'http://www.youtube-nocookie.com/embed/QHH3iSeDBLo',
    'https://www.youtube-nocookie.com.evil.test/embed/QHH3iSeDBLo',
    'https://www.youtube-nocookie.com@evil.test/embed/QHH3iSeDBLo',
    'https://www.youtube-nocookie.com/embed/QHH3iSeDBLo?autoplay=1',
    'https://www.youtube-nocookie.com/embed/short',
    'javascript:alert(1)', '<iframe src="https://example.com"></iframe>', None,
])
def test_reject_unapproved_video_sources(url):
    with pytest.raises(ValueError):
        validate_notes(json.dumps({'ops': [{'insert': {'video': url}}]}))


@pytest.mark.parametrize('attributes', [{'width': '50'}, {'imageAlign': 'center'}, {'bold': True}])
def test_reject_video_attributes(attributes):
    with pytest.raises(ValueError):
        validate_notes(json.dumps({'ops': [{'insert': {'video': 'https://www.youtube-nocookie.com/embed/QHH3iSeDBLo'},
                                         'attributes': attributes}]}))


def test_picture_size_and_alignment_are_preserved():
    delta = {'ops': [{'insert': {'image': PICTURE},
                      'attributes': {'width': '65', 'imageAlign': 'center'}}, {'insert': '\n'}]}
    assert json.loads(validate_notes(json.dumps(delta))) == delta


@pytest.mark.parametrize('url', [
    'https://example.com/lesson?part=1#code',
    'http://127.0.0.1:5000/reference',
    'mailto:student@example.com',
])
def test_safe_note_links_are_preserved(url):
    delta = {'ops': [{'insert': 'Reference', 'attributes': {'link': url}}, {'insert': '\n'}]}
    assert json.loads(validate_notes(json.dumps(delta))) == delta


@pytest.mark.parametrize('url', [
    'javascript:alert(1)', 'data:text/html,bad', '//example.com', '/relative',
    'https://', 'https://example.com/bad path', '', 3,
])
def test_reject_unsafe_note_links(url):
    with pytest.raises(ValueError):
        validate_notes(json.dumps({'ops': [{'insert': 'Bad', 'attributes': {'link': url}}]}))


@pytest.mark.parametrize('attributes', [
    {'width': '20'}, {'width': '101'}, {'width': '51'}, {'width': 50},
    {'imageAlign': 'justify'}, {'width': '50', 'bold': True},
])
def test_reject_invalid_picture_formatting(attributes):
    with pytest.raises(ValueError):
        validate_notes(json.dumps({'ops': [{'insert': {'image': PICTURE}, 'attributes': attributes}]}))


@pytest.mark.parametrize('image', [
    'data:image/svg+xml;base64,PHN2Zz4=',
    'data:image/png;base64,broken=',
    'data:image/png;base64,' + base64.b64encode(b'not a picture').decode(),
    'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (2 * 1024 * 1024)).decode(),
    'https://example.com/picture.png',
], ids=['svg', 'encoding', 'signature', 'size', 'remote'])
def test_reject_unsupported_pictures(image):
    with pytest.raises(ValueError):
        validate_notes(json.dumps({'ops': [{'insert': {'image': image}}]}))


def test_notes_combined_picture_budget():
    image = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'x' * (2 * 1024 * 1024 - 8)).decode()
    with pytest.raises(ValueError):
        validate_notes(json.dumps({'ops': [{'insert': {'image': image}}] * 4}))


@pytest.mark.parametrize('status', ['ACTIVE', 'COMPLETED'])
def test_notes_save_reload_update_and_clear(app, client, status):
    session = Session(project=Project(name='Game', path='archive:notes', is_archive=True),
                      start_commit_hash='a' * 40, status=status, ended_at=utcnow())
    db.session.add(session)
    db.session.commit()
    url = f'/api/sessions/{session.id}/notes'
    headers = {'X-GameLearn-Token': app.config['CODEX_BRIDGE_CSRF_TOKEN']}
    delta = {'ops': [{'insert': 'My observation', 'attributes': {'bold': True}}, {'insert': '\n'}]}
    for content in [delta, {'ops': [{'insert': '</script><script>alert(1)</script>\n'}]}]:
        assert client.post(url, json={'notes': json.dumps(content)}, headers=headers).status_code == 200
        db.session.expire_all()
        assert session.notes_delta == content
        page = f'/sessions/{session.id}' + ('/summary' if status == 'COMPLETED' else '')
        html = client.get(page).get_data(as_text=True)
        assert 'My notes' in html
        assert '/static/vendor/quill-2.0.3.js' in html
        assert '</script><script>alert(1)</script>' not in html
        assert 'notes' not in build_explanation_context(session)['current_session']
    assert client.post(url, json={'notes': json.dumps({'ops': [{'insert': '\n'}]})}, headers=headers).status_code == 200
    db.session.expire_all()
    assert session.notes is None
    assert session.events == []


@pytest.mark.parametrize('content', [
    'bad json', '[]', '{"ops":[{"insert":{"image":"https://example.com"}}]}',
    '{"ops":[{"insert":"text","attributes":{"link":"javascript:alert(1)"}}]}',
    '{"ops":[{"retain":1}]}', '{"ops":[{"insert":"text","attributes":{"header":true}}]}',
    'x' * 100001,
], ids=['json', 'array', 'embed', 'link', 'retain', 'header', 'oversized'])
def test_reject_invalid_notes(content):
    with pytest.raises(ValueError):
        validate_notes(content)


def test_notes_token_validation_and_backup_roundtrip(app, client):
    session = Session(project=Project(name='Game', path='archive:notes', is_archive=True),
                      start_commit_hash='a' * 40, status='COMPLETED', ended_at=utcnow(),
                      notes=json.dumps({'ops': [{'insert': 'Heading'}, {'insert': '\n', 'attributes': {'header': 2}}]}))
    db.session.add(session)
    db.session.commit()
    url = f'/api/sessions/{session.id}/notes'
    assert client.post(url, json={'notes': None}).status_code == 403
    headers = {'X-GameLearn-Token': app.config['CODEX_BRIDGE_CSRF_TOKEN']}
    for data in [[], {'notes': 'invalid'}, {'notes': None, 'extra': True}]:
        assert client.post(url, json=data, headers=headers).status_code == 400
    raw = export_project(session.project)
    restored = import_project(raw)
    assert restored.sessions[0].notes_delta == session.notes_delta
    payload = json.loads(raw)
    payload['sessions'][0]['session']['notes'] = '{"ops":[{"insert":{"image":"bad"}}]}'
    with pytest.raises(BackupError):
        import_project(json.dumps(payload).encode())
    assert client.get('/static/vendor/quill-2.0.3.js').status_code == 200
    assert client.get('/static/vendor/quill-2.0.3.snow.css').status_code == 200
