#!/bin/sh
# Timeline cards display changed paths; session diffs include colored lines and text export.
# Remove registered projects from their GameLearn page; Unity files remain untouched.
# Forgotten sessions can be recovered from the project page using a Git change preview.
set -eu

GAMELEARN_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$GAMELEARN_ROOT"

if ! command -v codex >/dev/null 2>&1; then
    echo "The Codex CLI must be installed and available on PATH." >&2
    exit 1
fi
if ! command -v node >/dev/null 2>&1; then
    echo "Node.js must be installed and available on PATH for local Mermaid syntax validation." >&2
    exit 1
fi

UV_VERSION="0.12.6"
TOOLS_DIR="$GAMELEARN_ROOT/.tools"
UV_DIR="$TOOLS_DIR/uv"
UV_EXECUTABLE="$UV_DIR/uv"
PYTHON_DIR="$TOOLS_DIR/python"
CACHE_DIR="$TOOLS_DIR/uv-cache"
VENV_PYTHON="$GAMELEARN_ROOT/.venv/bin/python"

mkdir -p "$TOOLS_DIR"

if [ ! -x "$UV_EXECUTABLE" ]; then
    echo "Downloading the project-local uv bootstrap..."
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | UV_UNMANAGED_INSTALL="$UV_DIR" UV_NO_MODIFY_PATH=1 sh
fi

if [ ! -x "$UV_EXECUTABLE" ]; then
    echo "The project-local uv bootstrap could not be installed." >&2
    exit 1
fi

export UV_PYTHON_INSTALL_DIR="$PYTHON_DIR"
export UV_CACHE_DIR="$CACHE_DIR"
export UV_NO_MODIFY_PATH=1

echo "Preparing a project-local Python 3.11 runtime..."
"$UV_EXECUTABLE" python install 3.11 --no-bin

NEEDS_VENV=0
if [ ! -x "$VENV_PYTHON" ]; then
    NEEDS_VENV=1
elif ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
    NEEDS_VENV=1
fi

if [ "$NEEDS_VENV" -eq 1 ]; then
    echo "Creating the GameLearn virtual environment..."
    "$UV_EXECUTABLE" venv --clear --managed-python --python 3.11 .venv
fi

if [ "${1:-}" != "--skip-sync" ]; then
    echo "Installing GameLearn dependencies into .venv..."
    "$UV_EXECUTABLE" pip install --python "$VENV_PYTHON" -r requirements.txt
fi

echo
echo "GameLearn is starting at http://127.0.0.1:5000"
echo "Choose the learning-page model and reasoning in the session summary; defaults are the latest model and Low reasoning. Tutor chat stays Low."
echo "Learning pages are created by dedicated Codex tasks in the background."
echo "Codex sign-in is checked through the CLI App Server using this terminal user's normal access."
echo "Tutor chats and worksheet feedback are saved locally and use dedicated read-only Codex tasks."
echo "Session numbering starts at 1 for each project."
echo "Optionally save or edit the session prompt and My notes (including pictures and YouTube links or embed codes) on an active session or completed summary."
echo "Learning guides use step-by-step, student-friendly explanations."
echo "Learning activities focus on Unity C# changes. Supporting scripts are excluded from lessons."
echo "Press Ctrl+C in this terminal to stop it."
echo "Completed summaries offer a Session menu for learning pages and notes. Export all sessions from a project page; import a project backup and rename sessions before restoring."
exec "$VENV_PYTHON" app.py
