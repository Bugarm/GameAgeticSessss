import json

import pytest

from gamelearn.services.learning_page_service import (
    LearningPageManifestError,
    build_learning_page_artifact,
    learning_page_output_schema,
    validate_learning_page_manifest,
)


def lesson_context():
    return {
        "current_session": {
            "file_changes": [
                {
                    "path": "Assets/Player.cs",
                    "change_type": "MODIFIED",
                    "unified_diff": (
                        "@@ -1,2 +1,4 @@\n"
                        " public class Player {\n"
                        "+    int speed = 5;\n"
                        "+    bool grounded = true;\n"
                        " }\n"
                    ),
                }
            ]
        }
    }


def lesson_manifest():
    steps = []
    for index in range(1, 5):
        step = {
            "title": f"Trace change {index}",
            "what_git_shows": ["The agent added a grounded movement safeguard."],
            "how_it_works": ["The value is checked before movement continues."],
            "why_agent_probably_did_this": ["This probably prevents movement in an invalid state."],
            "file_paths": ["Assets/Player.cs"],
            "snippet_references": [],
            "research": [],
            "diagram": None,
            "follow_ups": [],
        }
        steps.append(step)
    steps[0]["snippet_references"] = [
        {
            "path": "Assets/Player.cs",
            "symbol": "Player",
            "highlights": ["speed", "grounded"],
        }
    ]
    steps[1]["diagram"] = {
        "definition": (
            "flowchart TD\n"
            "accTitle: Movement safeguard\n"
            "accDescr: Grounded state controls whether movement continues.\n"
            "A[Check grounded] --> B{Grounded?}\n"
            "B -- Yes --> C[Move]"
        )
    }
    steps[2]["follow_ups"] = [
        {"label": "Ask about the safeguard", "question": "Why check grounded first?"}
    ]
    return {
        "schema_version": 5,
        "title": "Understanding the movement safeguard",
        "introduction": ["This lesson follows the important code change."],
        "steps": steps,
        "worksheet": {
            "introduction": "Read the code and explain what it does.",
            "items": [
                {
                    "type": "open",
                    "question": "How do the speed and grounded values control player movement?",
                    "path": "Assets/Player.cs",
                    "hint": "Trace the grounded value before the movement branch.",
                },
                {
                    "type": "multiple_choice",
                    "question": "What value controls the safeguard?",
                    "path": "Assets/Player.cs",
                    "options": ["The grounded flag.", "The class name.", "The file path."],
                },
                {
                    "type": "multiple_choice",
                    "question": "Why is the check useful?",
                    "options": [
                        "It prevents invalid movement.",
                        "It renames the file.",
                        "It deletes the player.",
                    ],
                },
            ],
        },
    }


def test_build_learning_page_artifact_uses_repository_template(tmp_path):
    from gamelearn import create_app

    app = create_app({"TESTING": True, "START_SESSION_MONITOR": False})
    output = tmp_path / "lesson.html"
    manifest = lesson_manifest()
    manifest["title"] = "Understanding <script>alert('no')</script> safely"
    build_learning_page_artifact(
        json.dumps(manifest),
        output,
        lesson_context(),
        app.root_path + "/templates",
    )

    rendered = output.read_text(encoding="utf-8")
    assert rendered.count("data-gamelearn-step=") == 5
    assert rendered.count("data-gamelearn-step-jump=") == 5
    assert 'class="gl-wizard-panel"' in rendered
    assert "data-gamelearn-previous" in rendered
    assert "data-gamelearn-next" in rendered
    assert "flowchart TD" in rendered
    assert "accTitle: Movement safeguard" in rendered
    assert "language-csharp" in rendered
    assert 'data-gamelearn-highlight-lines="2,3"' in rendered
    assert "Highlighted lines match this step's explanation." in rendered
    assert "&lt;script&gt;alert" in rendered
    assert "<script>alert" not in rendered
    assert "<script" not in rendered
    assert "<style" not in rendered

def test_manifest_rejects_unknown_paths_and_does_not_accept_echoed_code():
    manifest = lesson_manifest()
    manifest["steps"][0]["file_paths"] = ["Assets/Scene.unity"]
    with pytest.raises(LearningPageManifestError, match="eligible recorded code path"):
        validate_learning_page_manifest(json.dumps(manifest), lesson_context())

    manifest = lesson_manifest()
    manifest["steps"][0]["snippets"] = [
        {"path": "Assets/Player.cs", "code": "DestroyEverything();"}
    ]
    with pytest.raises(LearningPageManifestError, match="missing or unexpected fields"):
        validate_learning_page_manifest(json.dumps(manifest), lesson_context())


def test_manifest_fills_snippet_slot_from_the_stored_diff():
    normalized = validate_learning_page_manifest(json.dumps(lesson_manifest()), lesson_context())

    assert normalized["steps"][0]["snippets"] == [
        {
            "path": "Assets/Player.cs",
            "code": (
                "public class Player {\n"
                "    int speed = 5;\n"
                "    bool grounded = true;\n"
                "}"
            ),
            "language": "csharp",
            "highlight_lines": [2, 3],
        }
    ]


def test_stored_snippet_uses_only_the_referenced_method_without_stray_braces():
    context = lesson_context()
    context["current_session"]["file_changes"][0]["unified_diff"] = (
        "@@ -1,2 +1,24 @@\n"
        " }\n"
        " }\n"
        "+public static void RenameRoot()\n"
        "+{\n"
        "+    var typo = GameObject.Find(\"ForegroundGrasss\");\n"
        "+    var good = GameObject.Find(RootName);\n"
        "+    if (typo != null && good == null)\n"
        "+    {\n"
        "+        typo.name = RootName;\n"
        "+    }\n"
        "+}\n"
        "+\n"
        "+public static void RunFromMenu()\n"
        "+{\n"
        "+    RenameRoot();\n"
        "+}\n"
    )
    manifest = lesson_manifest()
    manifest["schema_version"] = 4
    manifest["steps"][0]["snippet_references"][0].pop("highlights")
    manifest["steps"][0]["snippet_references"][0]["symbol"] = "RenameRoot"

    normalized = validate_learning_page_manifest(json.dumps(manifest), context)
    code = normalized["steps"][0]["snippets"][0]["code"]

    assert code.startswith("public static void RenameRoot()")
    assert "ForegroundGrasss" in code
    assert code.endswith("}")
    assert not code.startswith("}")
    assert "RunFromMenu" not in code


def test_current_manifest_renders_the_complete_method_and_derives_highlight_lines():
    context = lesson_context()
    context["current_session"]["file_changes"][0]["unified_diff"] = (
        "@@ -1,2 +1,14 @@\n"
        "+public static void RenameRoot()\n"
        "+{\n"
        "+    var scene = EditorSceneManager.OpenScene(ScenePath);\n"
        "+    var cam = Camera.main;\n"
        "+    var camBefore = cam.transform.position;\n"
        "+    RenameTheObject();\n"
        "+    if (cam.transform.position != camBefore)\n"
        "+        throw new Exception(\"Camera moved.\");\n"
        "+}\n"
        "+\n"
        "+public static void Unrelated() {}\n"
    )
    manifest = lesson_manifest()
    manifest["steps"][0]["snippet_references"][0] = {
        "path": "Assets/Player.cs",
        "symbol": "RenameRoot",
        "highlights": ["OpenScene", "camBefore"],
    }

    normalized = validate_learning_page_manifest(json.dumps(manifest), context)
    snippet = normalized["steps"][0]["snippets"][0]

    assert snippet["code"].startswith("public static void RenameRoot()")
    assert snippet["code"].endswith("}")
    assert "RenameTheObject" in snippet["code"]
    assert "Unrelated" not in snippet["code"]
    assert snippet["highlight_lines"] == [3, 5, 7]


def test_stored_snippet_uses_nearby_statements_for_a_local_variable_reference():
    context = lesson_context()
    context["current_session"]["file_changes"][0]["unified_diff"] = (
        "@@ -1,2 +1,18 @@\n"
        "+public static void RenameRoot()\n"
        "+{\n"
        "+    var cam = Camera.main;\n"
        "+    var camBefore = cam.transform.position;\n"
        "+    RenameTheObject();\n"
        "+    var camAfter = cam.transform.position;\n"
        "+    if (camAfter != camBefore)\n"
        "+        throw new Exception(\"Camera moved\");\n"
        "+    SaveScene();\n"
        "+}\n"
        "+\n"
        "+public static void RunFromMenu()\n"
        "+{\n"
        "+    RenameRoot();\n"
        "+}\n"
    )
    manifest = lesson_manifest()
    manifest["schema_version"] = 4
    manifest["steps"][0]["snippet_references"][0].pop("highlights")
    manifest["steps"][0]["snippet_references"][0]["symbol"] = "camAfter"

    normalized = validate_learning_page_manifest(json.dumps(manifest), context)
    code = normalized["steps"][0]["snippets"][0]["code"]

    assert "camAfter" in code
    assert "Camera moved" in code
    assert "public static void RenameRoot" not in code
    assert "RunFromMenu" not in code


@pytest.mark.parametrize("symbol", ["typo", "good"])
def test_local_reference_keeps_the_complete_destroy_or_rename_control_block(symbol):
    context = lesson_context()
    context["current_session"]["file_changes"][0]["unified_diff"] = (
        "@@ -1,2 +1,18 @@\n"
        "+Quaternion camRotBefore = cam.transform.rotation;\n"
        "+var typo = GameObject.Find(\"ForegroundGrasss\");\n"
        "+var good = GameObject.Find(RootName);\n"
        "+if (typo != null)\n"
        "+{\n"
        "+    if (good != null && good != typo)\n"
        "+        UnityEngine.Object.DestroyImmediate(typo);\n"
        "+    else\n"
        "+        typo.name = RootName;\n"
        "+}\n"
        "+var root = GameObject.Find(RootName);\n"
    )
    manifest = lesson_manifest()
    manifest["schema_version"] = 4
    manifest["steps"][0]["snippet_references"][0].pop("highlights")
    manifest["steps"][0]["snippet_references"][0]["symbol"] = symbol

    normalized = validate_learning_page_manifest(json.dumps(manifest), context)
    code = normalized["steps"][0]["snippets"][0]["code"]

    assert "DestroyImmediate(typo)" in code
    assert "else" in code
    assert "typo.name = RootName" in code
    assert code.endswith("}")
    assert "camRotBefore" not in code
    assert "var root" not in code


def test_local_reference_keeps_separated_declaration_and_later_check():
    context = lesson_context()
    context["current_session"]["file_changes"][0]["unified_diff"] = (
        "@@ -1,2 +1,20 @@\n"
        "+var cam = Camera.main;\n"
        "+Vector3 camBefore = cam.transform.position;\n"
        "+Quaternion camRotBefore = cam.transform.rotation;\n"
        "+var typo = GameObject.Find(\"ForegroundGrasss\");\n"
        "+RenameTheObject();\n"
        "+if (cam != null &&\n"
        "+    ((cam.transform.position - camBefore).sqrMagnitude > 0.0001f ||\n"
        "+     Quaternion.Angle(camRotBefore, cam.transform.rotation) > 0.05f))\n"
        "+    throw new Exception(\"Camera moved.\");\n"
        "+SaveScene();\n"
    )
    manifest = lesson_manifest()
    manifest["schema_version"] = 4
    manifest["steps"][0]["snippet_references"][0].pop("highlights")
    manifest["steps"][0]["snippet_references"][0]["symbol"] = "camRotBefore"

    normalized = validate_learning_page_manifest(json.dumps(manifest), context)
    code = normalized["steps"][0]["snippets"][0]["code"]

    assert "Quaternion camRotBefore" in code
    assert "Quaternion.Angle(camRotBefore" in code
    assert "Camera moved" in code
    assert "..." in code


def test_open_question_is_specific_simple_code_explanation():
    normalized = validate_learning_page_manifest(json.dumps(lesson_manifest()), lesson_context())

    question = normalized["worksheet"]["items"][0]["question"]
    assert question == "How do the speed and grounded values control player movement?"
    assert "design" not in question.lower()


@pytest.mark.parametrize(
    "question",
    [
        "Explain what the changed code does?",
        "Design a safer movement system?",
        "How does this work",
    ],
)
def test_open_question_rejects_generic_design_or_non_question_wording(question):
    manifest = lesson_manifest()
    manifest["worksheet"]["items"][0]["question"] = question

    with pytest.raises(LearningPageManifestError, match="focused code-explanation question"):
        validate_learning_page_manifest(json.dumps(manifest), lesson_context())


def test_output_schema_constrains_paths_and_manifest_shape():
    schema = learning_page_output_schema(lesson_context())

    assert schema["properties"]["schema_version"] == {"type": "integer", "const": 5}
    assert schema["properties"]["steps"]["minItems"] == 4
    step = schema["properties"]["steps"]["items"]
    assert step["additionalProperties"] is False
    snippet_reference = step["properties"]["snippet_references"]["items"]
    assert snippet_reference["properties"]["path"]["enum"] == ["Assets/Player.cs"]
    assert snippet_reference["properties"]["symbol"]["pattern"] == (
        r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    assert snippet_reference["properties"]["highlights"]["minItems"] == 1
    assert snippet_reference["properties"]["highlights"]["maxItems"] == 6
    item_types = schema["properties"]["worksheet"]["properties"]["items"]["items"]["anyOf"]
    assert item_types[0]["properties"]["type"] == {"type": "string", "const": "open"}
    assert item_types[1]["properties"]["type"] == {
        "type": "string",
        "const": "multiple_choice",
    }


def test_manifest_parser_accepts_json_wrapped_in_harmless_commentary():
    wrapped = "Here is the lesson data:\n" + json.dumps(lesson_manifest()) + "\nDone."

    normalized = validate_learning_page_manifest(wrapped, lesson_context())

    assert normalized["title"] == "Understanding the movement safeguard"


def test_legacy_manifest_still_rejects_invented_snippets():
    manifest = lesson_manifest()
    manifest["schema_version"] = 1
    for step in manifest["steps"]:
        step["snippets"] = []
        step.pop("snippet_references")
    manifest["steps"][0]["snippets"] = [
        {"path": "Assets/Player.cs", "code": "DestroyEverything();"}
    ]

    with pytest.raises(LearningPageManifestError, match="not present in the stored current diff"):
        validate_learning_page_manifest(json.dumps(manifest), lesson_context())


def test_manifest_rejects_unsafe_or_inaccessible_mermaid():
    manifest = lesson_manifest()
    manifest["steps"][1]["diagram"]["definition"] = "flowchart TD\nA --> B"
    with pytest.raises(LearningPageManifestError, match="accessible title or description"):
        validate_learning_page_manifest(json.dumps(manifest), lesson_context())

    manifest = lesson_manifest()
    manifest["steps"][1]["diagram"]["definition"] += '\nclick A href "https://example.com"'
    with pytest.raises(LearningPageManifestError, match="unsafe directives"):
        validate_learning_page_manifest(json.dumps(manifest), lesson_context())
