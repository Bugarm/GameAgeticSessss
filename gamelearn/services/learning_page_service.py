"""Validate Codex lesson manifests and render them with GameLearn's fixed shell."""

import json
import os
from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape

from gamelearn.services.code_files import LEARNING_CODE_FILE_EXTENSIONS


MAX_MANIFEST_CHARACTERS = 100_000
MAX_TEXT_CHARACTERS = 4_000
MAX_DIAGRAM_CHARACTERS = 4_000
MAX_STORED_SNIPPET_CHARACTERS = 30_000
MAX_SNIPPET_SYMBOL_CHARACTERS = 120
OPEN_EXPLANATION_QUESTION = (
    "Explain what the changed code does. Describe how it works step by step in your own words."
)
ALLOWED_DIAGRAM_DECLARATIONS = (
    "classDiagram",
    "flowchart ",
    "sequenceDiagram",
    "stateDiagram",
    "stateDiagram-v2",
)
LANGUAGES_BY_EXTENSION = {
    ".c": "c",
    ".cginc": "hlsl",
    ".compute": "hlsl",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".glsl": "glsl",
    ".glslinc": "glsl",
    ".h": "c",
    ".hlsl": "hlsl",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".kt": "kotlin",
    ".py": "python",
    ".shader": "hlsl",
    ".swift": "swift",
    ".ts": "typescript",
    ".uss": "css",
    ".uxml": "xml",
}


class LearningPageManifestError(RuntimeError):
    """A safe failure caused by an invalid generated lesson manifest."""


def learning_page_output_schema(context):
    """Return the strict App Server output schema for the compact lesson manifest."""
    allowed_paths, _ = _evidence_sources(context)
    path_schema = {"type": "string", "enum": sorted(allowed_paths)}
    paragraph_list = {
        "type": "array",
        "minItems": 1,
        "maxItems": 3,
        "items": {"type": "string", "maxLength": MAX_TEXT_CHARACTERS},
    }
    step_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "what_git_shows",
            "how_it_works",
            "why_agent_probably_did_this",
            "file_paths",
            "snippet_references",
            "research",
            "diagram",
            "follow_ups",
        ],
        "properties": {
            "title": {"type": "string", "maxLength": 120},
            "what_git_shows": paragraph_list,
            "how_it_works": paragraph_list,
            "why_agent_probably_did_this": paragraph_list,
            "file_paths": {
                "type": "array",
                "maxItems": 3,
                "items": path_schema,
            },
            "snippet_references": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "symbol", "highlights"],
                    "properties": {
                        "path": path_schema,
                        "symbol": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_SNIPPET_SYMBOL_CHARACTERS,
                            "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$",
                        },
                        "highlights": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 6,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_SNIPPET_SYMBOL_CHARACTERS,
                                "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$",
                            },
                        },
                    },
                },
            },
            "research": {
                "type": "array",
                "maxItems": 2,
                "items": {"type": "string", "maxLength": MAX_TEXT_CHARACTERS},
            },
            "diagram": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["definition"],
                        "properties": {
                            "definition": {
                                "type": "string",
                                "maxLength": MAX_DIAGRAM_CHARACTERS,
                            }
                        },
                    },
                    {"type": "null"},
                ]
            },
            "follow_ups": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "question"],
                    "properties": {
                        "label": {"type": "string", "maxLength": 100},
                        "question": {"type": "string", "maxLength": 500},
                    },
                },
            },
        },
    }
    open_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "question", "path", "hint"],
        "properties": {
            "type": {"type": "string", "const": "open"},
            "question": {"type": "string", "maxLength": 280},
            "path": path_schema,
            "hint": {"type": "string", "maxLength": 500},
        },
    }
    multiple_choice_item = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "question", "options", "path"],
        "properties": {
            "type": {"type": "string", "const": "multiple_choice"},
            "question": {"type": "string", "maxLength": 280},
            "options": {
                "type": "array",
                "minItems": 3,
                "maxItems": 4,
                "items": {"type": "string", "maxLength": MAX_TEXT_CHARACTERS},
            },
            "path": {"anyOf": [path_schema, {"type": "null"}]},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "title", "introduction", "steps", "worksheet"],
        "properties": {
            "schema_version": {"type": "integer", "const": 5},
            "title": {"type": "string", "maxLength": 180},
            "introduction": paragraph_list,
            "steps": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": step_schema,
            },
            "worksheet": {
                "type": "object",
                "additionalProperties": False,
                "required": ["introduction", "items"],
                "properties": {
                    "introduction": {"type": "string", "maxLength": 500},
                    "items": {
                        "type": "array",
                        "minItems": 3,
                        "maxItems": 5,
                        "items": {"anyOf": [open_item, multiple_choice_item]},
                    },
                },
            },
        },
    }


def build_learning_page_artifact(raw_manifest, output_path, context, template_root):
    """Parse, validate, and atomically render one generated learning page."""
    manifest = validate_learning_page_manifest(raw_manifest, context)
    environment = Environment(
        loader=FileSystemLoader(str(Path(template_root))),
        autoescape=select_autoescape(("html", "xml")),
        keep_trailing_newline=True,
    )
    rendered = environment.get_template("learning_page_fragment.html").render(
        lesson=manifest,
        step_count=5,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8", newline="\n")
    os.replace(temporary, destination)
    return manifest


def validate_learning_page_manifest(raw_manifest, context):
    """Return a normalized, render-safe lesson manifest."""
    payload = _parse_manifest(raw_manifest)
    _require_keys(payload, {"schema_version", "title", "introduction", "steps", "worksheet"})
    schema_version = payload["schema_version"]
    if schema_version not in {1, 2, 3, 4, 5}:
        raise LearningPageManifestError("Codex returned an unsupported lesson format.")

    allowed_paths, current_diffs = _evidence_sources(context)
    normalized = {
        "schema_version": schema_version,
        "title": _text(payload["title"], "title", 180),
        "introduction": _text_list(payload["introduction"], "introduction", 1, 3),
        "steps": [],
    }
    steps = payload["steps"]
    if not isinstance(steps, list) or len(steps) != 4:
        raise LearningPageManifestError("The lesson must contain exactly four teaching steps.")
    for index, step in enumerate(steps, start=1):
        normalized["steps"].append(
            _normalize_step(step, index, schema_version, allowed_paths, current_diffs)
        )
    normalized["worksheet"] = _normalize_worksheet(
        payload["worksheet"], allowed_paths, schema_version
    )
    return normalized


def _parse_manifest(raw_manifest):
    if not isinstance(raw_manifest, str) or not raw_manifest.strip():
        raise LearningPageManifestError("Codex returned an empty lesson manifest.")
    if len(raw_manifest) > MAX_MANIFEST_CHARACTERS:
        raise LearningPageManifestError("The generated lesson manifest was too large.")
    candidate = raw_manifest.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as first_error:
        object_start = candidate.find("{")
        if object_start < 0:
            raise LearningPageManifestError("Codex returned malformed lesson data.") from first_error
        try:
            payload, _ = json.JSONDecoder().raw_decode(candidate[object_start:])
        except json.JSONDecodeError as exc:
            raise LearningPageManifestError("Codex returned malformed lesson data.") from exc
    if not isinstance(payload, dict):
        raise LearningPageManifestError("Codex returned an invalid lesson data structure.")
    return payload


def _normalize_step(step, index, schema_version, allowed_paths, current_diffs):
    if not isinstance(step, dict):
        raise LearningPageManifestError(f"Teaching step {index} was not an object.")
    required = {"title", "what_git_shows", "how_it_works", "why_agent_probably_did_this"}
    if schema_version >= 4:
        snippet_field = "snippet_references"
    elif schema_version >= 2:
        snippet_field = "snippet_paths"
    else:
        snippet_field = "snippets"
    optional = {"file_paths", snippet_field, "research", "diagram", "follow_ups"}
    _require_keys(step, required, optional)
    paths = _path_list(step.get("file_paths", []), allowed_paths, f"step {index} file_paths", 3)
    if schema_version >= 5:
        snippet_references = step.get("snippet_references", [])
        if not isinstance(snippet_references, list) or len(snippet_references) > 2:
            raise LearningPageManifestError(
                f"Teaching step {index} had too many code snippet references."
            )
        normalized_snippets = [
            _stored_method_snippet_reference(item, allowed_paths, current_diffs, index)
            for item in snippet_references
        ]
    elif schema_version >= 4:
        snippet_references = step.get("snippet_references", [])
        if not isinstance(snippet_references, list) or len(snippet_references) > 2:
            raise LearningPageManifestError(
                f"Teaching step {index} had too many code snippet references."
            )
        normalized_snippets = [
            _stored_snippet_reference(item, allowed_paths, current_diffs, index)
            for item in snippet_references
        ]
    elif schema_version >= 2:
        snippet_paths = _path_list(
            step.get("snippet_paths", []),
            allowed_paths,
            f"step {index} snippet_paths",
            2,
        )
        normalized_snippets = [
            _stored_snippet(path, current_diffs.get(path, "")) for path in snippet_paths
        ]
        normalized_snippets = [snippet for snippet in normalized_snippets if snippet]
    else:
        snippets = step.get("snippets", [])
        if not isinstance(snippets, list) or len(snippets) > 2:
            raise LearningPageManifestError(f"Teaching step {index} had too many code snippets.")
        current_sources = {
            path: _current_diff_source(diff_text) for path, diff_text in current_diffs.items()
        }
        normalized_snippets = [
            _normalize_snippet(item, allowed_paths, current_sources, index) for item in snippets
        ]
    for snippet in normalized_snippets:
        if snippet["path"] not in paths:
            paths.append(snippet["path"])

    research = _text_list(step.get("research", []), f"step {index} research", 0, 2)
    follow_ups = step.get("follow_ups", [])
    if not isinstance(follow_ups, list) or len(follow_ups) > 2:
        raise LearningPageManifestError(f"Teaching step {index} had too many follow-up actions.")
    return {
        "number": index,
        "title": _text(step["title"], f"step {index} title", 120),
        "what_git_shows": _text_list(
            step["what_git_shows"], f"step {index} what_git_shows", 1, 3
        ),
        "how_it_works": _text_list(step["how_it_works"], f"step {index} how_it_works", 1, 3),
        "why_agent_probably_did_this": _text_list(
            step["why_agent_probably_did_this"],
            f"step {index} why_agent_probably_did_this",
            1,
            3,
        ),
        "file_paths": paths,
        "snippets": normalized_snippets,
        "research": research,
        "diagram": _normalize_diagram(step.get("diagram"), index),
        "follow_ups": [
            _normalize_follow_up(item, index, follow_index)
            for follow_index, item in enumerate(follow_ups, start=1)
        ],
    }


def _normalize_snippet(snippet, allowed_paths, current_sources, step_index):
    if not isinstance(snippet, dict):
        raise LearningPageManifestError(f"Teaching step {step_index} had an invalid code snippet.")
    _require_keys(snippet, {"path", "code"})
    path = _path(snippet["path"], allowed_paths, f"step {step_index} snippet path")
    code = _text(snippet["code"], f"step {step_index} snippet code", 3_000, preserve=True)
    if code not in current_sources.get(path, ""):
        raise LearningPageManifestError(
            f"A code snippet in teaching step {step_index} was not present in the stored current diff."
        )
    return {
        "path": path,
        "code": code,
        "language": LANGUAGES_BY_EXTENSION.get(Path(path).suffix.lower(), "text"),
    }


def _stored_snippet_reference(reference, allowed_paths, current_diffs, step_index):
    if not isinstance(reference, dict):
        raise LearningPageManifestError(
            f"Teaching step {step_index} had an invalid code snippet reference."
        )
    _require_keys(reference, {"path", "symbol"})
    path = _path(reference["path"], allowed_paths, f"step {step_index} snippet path")
    symbol = _text(
        reference["symbol"],
        f"step {step_index} snippet symbol",
        MAX_SNIPPET_SYMBOL_CHARACTERS,
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        raise LearningPageManifestError(
            f"Teaching step {step_index} used an invalid code snippet symbol."
        )
    snippet = _stored_snippet(path, current_diffs.get(path, ""), symbol)
    if snippet is None:
        raise LearningPageManifestError(
            f"The code symbol for teaching step {step_index} was not present in the stored current diff."
        )
    return snippet


def _stored_method_snippet_reference(reference, allowed_paths, current_diffs, step_index):
    if not isinstance(reference, dict):
        raise LearningPageManifestError(
            f"Teaching step {step_index} had an invalid code snippet reference."
        )
    _require_keys(reference, {"path", "symbol", "highlights"})
    path = _path(reference["path"], allowed_paths, f"step {step_index} snippet path")
    symbol = _code_identifier(
        reference["symbol"], f"step {step_index} snippet method or type"
    )
    highlights = reference["highlights"]
    if not isinstance(highlights, list) or not 1 <= len(highlights) <= 6:
        raise LearningPageManifestError(
            f"Teaching step {step_index} had the wrong number of code highlights."
        )
    normalized_highlights = [
        _code_identifier(value, f"step {step_index} code highlight") for value in highlights
    ]
    if len(set(normalized_highlights)) != len(normalized_highlights):
        raise LearningPageManifestError(f"Teaching step {step_index} repeated a code highlight.")

    snippet = _stored_snippet(
        path,
        current_diffs.get(path, ""),
        symbol,
        whole_declaration=True,
    )
    if snippet is None:
        raise LearningPageManifestError(
            f"The method or type for teaching step {step_index} was not complete in the stored current diff."
        )
    highlight_lines = _highlight_line_numbers(snippet["code"], normalized_highlights)
    if highlight_lines is None:
        raise LearningPageManifestError(
            f"A code highlight for teaching step {step_index} was not present in its stored method."
        )
    snippet["highlight_lines"] = highlight_lines
    return snippet


def _code_identifier(value, label):
    identifier = _text(value, label, MAX_SNIPPET_SYMBOL_CHARACTERS)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise LearningPageManifestError(f"The generated {label} was not a code identifier.")
    return identifier


def _highlight_line_numbers(code, identifiers):
    lines = code.splitlines()
    highlighted = set()
    for identifier in identifiers:
        pattern = re.compile(rf"\b{re.escape(identifier)}\b")
        matches = {index for index, line in enumerate(lines, start=1) if pattern.search(line)}
        if not matches:
            return None
        highlighted.update(matches)
    return sorted(highlighted)


def _stored_snippet(path, diff_text, symbol=None, whole_declaration=False):
    """Build a verified, bounded code excerpt locally from the stored current diff."""
    hunks = []
    current_lines = []
    has_addition = False
    in_hunk = False

    def finish_hunk():
        if current_lines:
            hunks.append((list(current_lines), has_addition))
        current_lines.clear()

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            finish_hunk()
            has_addition = False
            in_hunk = True
            continue
        if not in_hunk or line.startswith("-") or line.startswith("\\ No newline"):
            continue
        if line.startswith(("+", " ")):
            if line.startswith("+"):
                has_addition = True
            current_lines.append(line[1:])
    finish_hunk()

    selected_hunks = [lines for lines, added in hunks if added]
    if not selected_hunks and hunks:
        selected_hunks = [hunks[0][0]]
    if not selected_hunks:
        return None

    if symbol:
        focused = [
            _focused_symbol_excerpt(lines, symbol, require_declaration=True)
            for lines in selected_hunks
        ]
        focused = [excerpt for excerpt in focused if excerpt]
        if not focused and not whole_declaration:
            focused = [_focused_symbol_excerpt(lines, symbol) for lines in selected_hunks]
            focused = [excerpt for excerpt in focused if excerpt]
        if not focused:
            return None
        code = focused[0]
    else:
        code = "\n\n".join("\n".join(lines) for lines in selected_hunks).strip("\r\n")
    if not code.strip() or "\x00" in code:
        return None
    if len(code) > MAX_STORED_SNIPPET_CHARACTERS:
        code = code[:MAX_STORED_SNIPPET_CHARACTERS]
        if "\n" in code:
            code = code.rsplit("\n", 1)[0]
        code = code.rstrip()
    return {
        "path": path,
        "code": code,
        "language": LANGUAGES_BY_EXTENSION.get(Path(path).suffix.lower(), "text"),
    }


def _focused_symbol_excerpt(lines, symbol, require_declaration=False):
    """Return the smallest brace-delimited declaration containing a verified symbol."""
    symbol_pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    matching_lines = [index for index, line in enumerate(lines) if symbol_pattern.search(line)]
    if not matching_lines:
        return None

    blocks = []
    for start, line in enumerate(lines):
        type_declaration = re.search(
            r"\b(?:class|struct|interface|enum|record|namespace)\s+[A-Za-z_][A-Za-z0-9_]*",
            line,
        )
        if ("(" not in line and not type_declaration) or re.match(
            r"^\s*(?:if|for|foreach|while|switch|catch|using|return|throw)\b", line
        ):
            continue
        opening = _find_opening_brace(lines, start)
        if opening is None:
            continue
        end = _find_closing_brace(lines, opening[0], opening[1])
        if end is None:
            continue
        declaration_start = start
        while declaration_start > 0 and lines[declaration_start - 1].lstrip().startswith("["):
            declaration_start -= 1
        blocks.append((declaration_start, end, opening[0]))

    exact_blocks = []
    for start, end, opening_line in blocks:
        block_text = "\n".join(lines[start : end + 1])
        if not symbol_pattern.search(block_text):
            continue
        declaration = " ".join(lines[start : opening_line + 1])
        if re.search(
            rf"(?:\b{re.escape(symbol)}\s*\(|"
            rf"\b(?:class|struct|interface|enum|record|namespace)\s+{re.escape(symbol)}\b)",
            declaration,
        ):
            exact_blocks.append((start, end))

    if exact_blocks:
        start, end = min(exact_blocks, key=lambda item: item[1] - item[0])
        return "\n".join(lines[start : end + 1]).strip("\r\n")
    if require_declaration:
        return None

    first_match = matching_lines[0]
    related_control = _nearby_control_block(lines, first_match, symbol_pattern)
    if related_control:
        ranges = [(first_match, related_control[1] + 1, True)]
    else:
        ranges = [(max(0, first_match - 2), first_match + 1, False)]

    for line_index in matching_lines[1:]:
        if any(start <= line_index < end for start, end, _ in ranges):
            continue
        last_start, last_end, preserve_edges = ranges[-1]
        if line_index <= last_end + 2:
            ranges[-1] = (
                last_start,
                min(len(lines), line_index + 3),
                preserve_edges,
            )
            continue
        ranges.append(
            (max(0, line_index - 2), min(len(lines), line_index + 3), False)
        )
        if len(ranges) == 2:
            break

    excerpts = []
    for start, end, preserve_edges in ranges:
        excerpt = list(lines[start:end])
        if not preserve_edges:
            while excerpt and excerpt[0].strip() in {"{", "}"}:
                excerpt.pop(0)
            while excerpt and excerpt[-1].strip() in {"{", "}"}:
                excerpt.pop()
        if excerpt:
            excerpts.append("\n".join(excerpt).strip("\r\n"))
    return "\n\n...\n\n".join(excerpts) or None


def _nearby_control_block(lines, first_match, symbol_pattern):
    """Find the first complete braced control block related to a nearby symbol declaration."""
    control_pattern = re.compile(r"^\s*(?:if|else\s+if|for|foreach|while|switch)\b")
    for start in range(first_match, min(len(lines), first_match + 6)):
        if not control_pattern.match(lines[start]):
            continue
        opening = _find_opening_brace(lines, start)
        if opening is None:
            continue
        end = _find_closing_brace(lines, opening[0], opening[1])
        if end is None:
            continue
        if symbol_pattern.search("\n".join(lines[start : end + 1])):
            return start, end
    return None


def _find_opening_brace(lines, start):
    for line_index in range(start, min(len(lines), start + 5)):
        brace_positions = _code_brace_positions(lines[line_index], "{")
        if brace_positions:
            return line_index, brace_positions[0]
        if ";" in lines[line_index]:
            return None
    return None


def _find_closing_brace(lines, opening_line, opening_column):
    depth = 0
    for line_index in range(opening_line, len(lines)):
        for column, brace in _code_braces(lines[line_index]):
            if line_index == opening_line and column < opening_column:
                continue
            depth += 1 if brace == "{" else -1
            if depth == 0:
                return line_index
    return None


def _code_brace_positions(line, wanted):
    return [column for column, brace in _code_braces(line) if brace == wanted]


def _code_braces(line):
    """Return braces outside simple quoted strings and line comments."""
    result = []
    quote = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote:
            if character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "/" and index + 1 < len(line) and line[index + 1] == "/":
            break
        if character in "{}":
            result.append((index, character))
    return result


def _normalize_diagram(diagram, step_index):
    if diagram is None:
        return None
    if not isinstance(diagram, dict):
        raise LearningPageManifestError(f"Teaching step {step_index} had an invalid diagram.")
    _require_keys(diagram, {"definition"})
    definition = _text(
        diagram["definition"], f"step {step_index} diagram", MAX_DIAGRAM_CHARACTERS, preserve=True
    )
    if not definition.startswith(ALLOWED_DIAGRAM_DECLARATIONS):
        raise LearningPageManifestError(f"Teaching step {step_index} used an unsupported diagram type.")
    lowered = definition.lower()
    meaningful_lines = [line.strip() for line in definition.splitlines() if line.strip()]
    if (
        len(meaningful_lines) < 3
        or not meaningful_lines[1].lower().startswith("acctitle:")
        or not meaningful_lines[2].lower().startswith("accdescr:")
    ):
        raise LearningPageManifestError(
            f"Teaching step {step_index} diagram was missing its accessible title or description."
        )
    if re.search(
        r"\b(click|href|linkstyle|classdef|style)\b|<\/?(?:script|style|iframe)\b|%%\{",
        lowered,
    ):
        raise LearningPageManifestError(f"Teaching step {step_index} diagram used unsafe directives.")
    return {"definition": definition}


def _normalize_follow_up(item, step_index, follow_index):
    if not isinstance(item, dict):
        raise LearningPageManifestError(f"Teaching step {step_index} had an invalid follow-up action.")
    _require_keys(item, {"label", "question"})
    return {
        "label": _text(item["label"], f"step {step_index} follow-up {follow_index} label", 100),
        "question": _text(
            item["question"], f"step {step_index} follow-up {follow_index} question", 500
        ),
    }


def _normalize_worksheet(worksheet, allowed_paths, schema_version):
    if not isinstance(worksheet, dict):
        raise LearningPageManifestError("The worksheet was not an object.")
    _require_keys(worksheet, {"introduction", "items"})
    items = worksheet["items"]
    if not isinstance(items, list) or not 3 <= len(items) <= 6:
        raise LearningPageManifestError("The worksheet must contain between three and six items.")
    normalized = []
    open_count = 0
    multiple_choice_count = 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise LearningPageManifestError(f"Worksheet item {index} was not an object.")
        item_type = item.get("type")
        if item_type == "open":
            if schema_version >= 4:
                _require_keys(item, {"type", "question", "path", "hint"})
                question = _meaningful_open_question(item["question"], index)
            elif schema_version >= 3:
                _require_keys(item, {"type", "path", "hint"})
                question = OPEN_EXPLANATION_QUESTION
            else:
                _require_keys(item, {"type", "question", "path", "hint"})
                question = OPEN_EXPLANATION_QUESTION
            open_count += 1
            normalized.append(
                {
                    "type": "open",
                    "question": question,
                    "path": _path(item["path"], allowed_paths, f"worksheet item {index} path"),
                    "hint": _text(item["hint"], f"worksheet item {index} hint", 500),
                }
            )
        elif item_type == "multiple_choice":
            _require_keys(item, {"type", "question", "options"}, {"path"})
            options = _text_list(item["options"], f"worksheet item {index} options", 3, 4)
            if len(set(options)) != len(options):
                raise LearningPageManifestError(f"Worksheet item {index} repeated an answer option.")
            multiple_choice_count += 1
            normalized.append(
                {
                    "type": "multiple_choice",
                    "question": _text(item["question"], f"worksheet item {index} question", 280),
                    "path": _optional_path(
                        item.get("path"), allowed_paths, f"worksheet item {index} path"
                    ),
                    "options": options,
                }
            )
        else:
            raise LearningPageManifestError(f"Worksheet item {index} had an unsupported type.")
    if open_count < 1 or multiple_choice_count < 2:
        raise LearningPageManifestError(
            "The worksheet must contain at least one open response and two multiple-choice items."
        )
    return {
        "introduction": _text(worksheet["introduction"], "worksheet introduction", 500),
        "items": normalized,
    }


def _meaningful_open_question(value, item_index):
    question = _text(value, f"worksheet item {item_index} question", 280)
    lowered = question.lower()
    if (
        not question.endswith("?")
        or lowered.startswith("explain what the changed code does")
        or lowered.startswith("describe how the changed code works")
        or re.search(
            r"\b(?:design|create|implement|edit|modify|build)\b|\bmake\s+(?:a|an|the)\b",
            lowered,
        )
    ):
        raise LearningPageManifestError(
            f"Worksheet item {item_index} did not contain a focused code-explanation question."
        )
    return question


def _evidence_sources(context):
    current = context.get("current_session") or {}
    allowed_paths = set()
    diffs = {}
    for change in current.get("file_changes") or []:
        path = change.get("path")
        if (
            isinstance(path, str)
            and Path(path).suffix.lower() in LEARNING_CODE_FILE_EXTENSIONS
            and change.get("change_type") != "DELETED"
        ):
            allowed_paths.add(path)
            diffs[path] = change.get("unified_diff") or ""
    if not allowed_paths:
        raise LearningPageManifestError("The session did not contain eligible current code files.")
    return allowed_paths, diffs


def _current_diff_source(diff_text):
    lines = []
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            if lines and lines[-1] != "":
                lines.append("")
            in_hunk = True
            continue
        if not in_hunk or line.startswith("-") or line.startswith("\\ No newline"):
            continue
        if line.startswith(("+", " ")):
            lines.append(line[1:])
    return "\n".join(lines)


def _require_keys(value, required, optional=None):
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        raise LearningPageManifestError("Codex returned lesson data with missing or unexpected fields.")


def _text(value, label, maximum=MAX_TEXT_CHARACTERS, preserve=False):
    if not isinstance(value, str):
        raise LearningPageManifestError(f"The generated {label} was not text.")
    cleaned = value.strip() if preserve else " ".join(value.split())
    if not cleaned or len(cleaned) > maximum or "\x00" in cleaned:
        raise LearningPageManifestError(f"The generated {label} was empty or too long.")
    return cleaned


def _text_list(value, label, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise LearningPageManifestError(f"The generated {label} had the wrong number of entries.")
    return [_text(item, label) for item in value]


def _path_list(value, allowed_paths, label, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise LearningPageManifestError(f"The generated {label} had too many entries.")
    result = []
    for item in value:
        path = _path(item, allowed_paths, label)
        if path not in result:
            result.append(path)
    return result


def _path(value, allowed_paths, label):
    if not isinstance(value, str) or value not in allowed_paths:
        raise LearningPageManifestError(f"The generated {label} was not an eligible recorded code path.")
    return value


def _optional_path(value, allowed_paths, label):
    if value is None:
        return None
    return _path(value, allowed_paths, label)
