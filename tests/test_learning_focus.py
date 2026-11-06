import json

import pytest

from gamelearn import db
from gamelearn.models import Event, Project, Session, SessionFileChange, utcnow
from gamelearn.services.explanation_service import (
    build_explanation_context,
    build_lesson_render_context,
)
from gamelearn.services.learning_page_service import _evidence_sources


@pytest.mark.parametrize("profile", ["learning", "tutor"])
def test_unity_learning_excludes_supporting_scripts(app, tmp_path, profile):
    project = Project(name="Unity game", path=str(tmp_path))
    session = Session(
        project=project, start_commit_hash="a" * 40,
        status="COMPLETED", ended_at=utcnow(),
    )
    csharp = "UnityProject/Game/Assets/Player.cs"
    python = "Blender/scripts/export-for-unity.py"
    for path, source in [
        (csharp, "public class Player {}"),
        (python, "def run():\n    return 'supporting_script_marker'"),
    ]:
        lines = source.splitlines()
        session.file_changes.append(SessionFileChange(
            path=path, change_type="CREATED", additions=len(lines), deletions=0,
            diff_text=f"@@ -0,0 +1,{len(lines)} @@\n" +
            "".join("+" + line + "\n" for line in lines),
        ))
        session.events.append(Event(
            source="GIT", event_type="FILE_CREATED", title="File created",
            description=path, status="INFO", metadata_json=json.dumps({"path": path}),
        ))
    db.session.add(project)
    db.session.commit()

    context = build_explanation_context(session, profile=profile)
    assert [c["path"] for c in context["current_session"]["file_changes"]] == [csharp]
    assert [e["path"] for e in context["current_session"]["timeline"]] == [csharp]
    assert context["comparison"]["files_only_in_current"] == [csharp]
    assert "supporting_script_marker" not in json.dumps(context)
    assert context["current_session"]["non_code_change_summary"]["paths"] == [
        {"path": python, "change_type": "CREATED"}
    ]

    class EmptyBaseline:
        def file_at_commit(self, *args):
            return None

    rendered = build_lesson_render_context(session, git_service=EmptyBaseline())
    assert [c["path"] for c in rendered["current_session"]["file_changes"]] == [csharp]
    # Even an unfiltered context must not allow a generated Python reference.
    rendered["current_session"]["file_changes"].append({
        "path": python, "change_type": "CREATED", "unified_diff": "+def run(): pass",
    })
    assert _evidence_sources(rendered)[0] == {csharp}
