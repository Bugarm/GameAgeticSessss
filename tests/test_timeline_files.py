import json

from gamelearn.models import Event
from gamelearn.routes import _event_payload


def test_saved_status_paths_are_visible_in_timeline_and_polling(app):
    from datetime import datetime
    from flask import render_template_string

    event = Event(id=1, timestamp=datetime.now(), source='GIT', status='INFO',
                  event_type='GIT_STATUS', title='Changes detected',
                  metadata_json=json.dumps({'snapshot': {
                      'MODIFIED::Assets/<Player>.cs': 'one',
                      'RENAMED:Assets/Old.cs:Assets/New.cs': 'two',
                  }}))
    with app.app_context():
        files = _event_payload(event)['changed_files']
        assert files == [
            {'change_type': 'Modified', 'path': 'Assets/<Player>.cs', 'old_path': ''},
            {'change_type': 'Renamed', 'path': 'Assets/New.cs', 'old_path': 'Assets/Old.cs'},
        ]
        for name in ('session.html', 'session_summary.html'):
            source = app.jinja_loader.get_source(app.jinja_env, name)[0]
            start = source.index('{% if event.changed_files %}')
            end = source.index('</ul>{% endif %}', start) + len('</ul>{% endif %}')
            rendered = render_template_string(source[start:end], event=event)
            assert 'Assets/&lt;Player&gt;.cs' in rendered
            assert 'Assets/Old.cs' in rendered
            assert 'Assets/New.cs' in rendered


def test_status_file_list_tolerates_missing_or_invalid_metadata():
    for metadata in (None, 'invalid', '[]', '{"snapshot": []}', '{"snapshot": {"bad": "x"}}'):
        assert Event(event_type='GIT_STATUS', metadata_json=metadata).changed_files == []
