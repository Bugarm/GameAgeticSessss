from gamelearn import db
from gamelearn.models import Project, Session, SessionFileChange


def test_diff_highlighting_and_export(app, client, tmp_path):
    diff = '--- a/Player.cs\n+++ b/Player.cs\n@@ -1 +1 @@\n-old\n+<script>alert(1)</script>\n context\n'
    with app.app_context():
        session = Session(project=Project(name='Game', path=str(tmp_path)),
                          start_commit_hash='a' * 40, status='COMPLETED')
        session.file_changes = [
            SessionFileChange(path='Player.cs', change_type='MODIFIED', additions=1, deletions=1, diff_text=diff),
            SessionFileChange(path='Image.png', change_type='MODIFIED', is_binary=True, diff_text='binary secret'),
            SessionFileChange(path='Missing.cs', change_type='MODIFIED'),
        ]
        db.session.add(session)
        db.session.commit()
        session_id = session.id
    page = client.get(f'/sessions/{session_id}/diff').get_data(as_text=True)
    assert 'diff-meta">+++ b/Player.cs' in page
    assert 'diff-removed">-old' in page
    assert 'diff-added">+&lt;script&gt;' in page
    assert '<script>alert(1)</script>' not in page
    assert f'/sessions/{session_id}/diff/export' in page
    response = client.get(f'/sessions/{session_id}/diff/export')
    assert response.status_code == 200
    assert 'attachment;' in response.headers['Content-Disposition']
    exported = response.get_data(as_text=True)
    assert exported.startswith('This is a Unity game using a CLI workflow driven by a coding agent')
    assert "Make use of Mermaid UML diagrams wherever possible" in exported
    assert diff in exported
    assert 'binary secret' not in exported
    assert 'Binary file: content not included.' in exported
    assert 'No textual diff was available.' in exported
    assert str(tmp_path) not in exported


def test_diff_export_active_missing_and_empty_sessions(app, client, tmp_path):
    with app.app_context():
        session = Session(project=Project(name='Game', path=str(tmp_path)), start_commit_hash='a' * 40)
        db.session.add(session)
        db.session.commit()
        session_id = session.id
        assert client.get(f'/sessions/{session_id}/diff/export').status_code == 302
        session.status = 'COMPLETED'
        db.session.commit()
    assert 'No differences from the starting commit.' in client.get(f'/sessions/{session_id}/diff/export').get_data(as_text=True)
    assert client.get('/sessions/999999/diff/export').status_code == 404
