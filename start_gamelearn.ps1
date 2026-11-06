# Timeline cards display changed paths; session diffs include colored lines and text export.
# Remove registered projects from their GameLearn page; Unity files remain untouched.
# Forgotten sessions can be recovered from the project page using a Git change preview.
param(
    [switch]$SkipSync
)

$ErrorActionPreference = "Stop"
$GameLearnRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $GameLearnRoot

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    throw "The Codex CLI must be installed and available on PATH."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "Node.js must be installed and available on PATH for local Mermaid syntax validation."
}

$UvVersion = "0.12.6"
$ToolsDir = Join-Path $GameLearnRoot ".tools"
$UvDir = Join-Path $ToolsDir "uv"
$UvExecutable = Join-Path $UvDir "uv.exe"
$PythonDir = Join-Path $ToolsDir "python"
$CacheDir = Join-Path $ToolsDir "uv-cache"
$VenvPython = Join-Path $GameLearnRoot ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null

if (-not (Test-Path -LiteralPath $UvExecutable)) {
    Write-Host "Downloading the project-local uv bootstrap..."
    $env:UV_UNMANAGED_INSTALL = $UvDir
    $env:UV_NO_MODIFY_PATH = "1"
    $InstallerUrl = "https://astral.sh/uv/$UvVersion/install.ps1"
    $InstallerScript = Invoke-RestMethod -Uri $InstallerUrl
    Invoke-Expression $InstallerScript
}

if (-not (Test-Path -LiteralPath $UvExecutable)) {
    throw "The project-local uv bootstrap could not be installed."
}

$env:UV_PYTHON_INSTALL_DIR = $PythonDir
$env:UV_CACHE_DIR = $CacheDir
$env:UV_NO_MODIFY_PATH = "1"

Write-Host "Preparing a project-local Python 3.11 runtime..."
& $UvExecutable python install 3.11 --no-bin --no-registry
if ($LASTEXITCODE -ne 0) { throw "The local Python runtime could not be prepared." }

$NeedsVenv = -not (Test-Path -LiteralPath $VenvPython)
if (-not $NeedsVenv) {
    & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
    $NeedsVenv = $LASTEXITCODE -ne 0
}
if ($NeedsVenv) {
    Write-Host "Creating the GameLearn virtual environment..."
    & $UvExecutable venv --clear --managed-python --python 3.11 .venv
    if ($LASTEXITCODE -ne 0) { throw "The GameLearn virtual environment could not be created." }
}

if (-not $SkipSync) {
    Write-Host "Installing GameLearn dependencies into .venv..."
    & $UvExecutable pip install --python $VenvPython -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "GameLearn dependencies could not be installed." }
}

Write-Host ""
Write-Host "GameLearn is starting at http://127.0.0.1:5000" -ForegroundColor Green
Write-Host "Choose the learning-page model and reasoning in the session summary; defaults are the latest model and Low reasoning. Tutor chat stays Low."
Write-Host "Learning pages are created by dedicated Codex tasks in the background."
Write-Host "Codex sign-in is checked through the CLI App Server using this PowerShell user's normal access."
Write-Host "Tutor chats and worksheet feedback are saved locally and use dedicated read-only Codex tasks."
Write-Host "Session numbering starts at 1 for each project."
Write-Host "Optionally save or edit the session prompt and My notes (including pictures and YouTube links or embed codes) on an active session or completed summary."
Write-Host "Learning guides use step-by-step, student-friendly explanations."
Write-Host "Learning activities focus on Unity C# changes. Supporting scripts are excluded from lessons."
Write-Host "Press Ctrl+C in this window to stop it."
Write-Host "Completed summaries offer a Session menu for learning pages and notes. Export all sessions from a project page; import a project backup and rename sessions before restoring."
& $VenvPython app.py
