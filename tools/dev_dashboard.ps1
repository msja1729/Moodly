# Development only: run Ambient Mood from source with the mood trace on.
# The app serves the mood path dashboard and shows a "Mood path dashboard" link
# in the sidebar and the tray menu (http://127.0.0.1:8765).
#
#   .\tools\dev_dashboard.ps1
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:AMBIENT_MOOD_TRACE = Join-Path $root "data\mood_trace.jsonl"
& (Join-Path $root ".venv\Scripts\python.exe") -m frontend.app @args
