# Builds the Windows release.
#
#   .\packaging\build.ps1 -Version 1.0.0
#
# 1. checks the song catalog exists (data/catalog.db)
# 2. runs the tests
# 3. PyInstaller  -> dist\AmbientMood\AmbientMood.exe
# 4. Inno Setup   -> dist\AmbientMood-Setup-<version>.exe   (skipped if Inno Setup isn't installed)
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [switch]$SkipTests
)
# Native tools report through $LASTEXITCODE (checked after each); PowerShell 5.1 would treat their
# stderr notices as errors under ErrorActionPreference=Stop.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "No .venv found. Create it first: python -m venv .venv" }

if (-not (Test-Path "data\catalog.db")) {
    Write-Host "data\catalog.db is missing; importing the song catalog..."
    & $python -m backend.catalog import
    if ($LASTEXITCODE -ne 0) { throw "catalog import failed" }
}

& $python -m pip install --quiet --disable-pip-version-check -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

if (-not $SkipTests) {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "tests failed; not building a release" }
}

& $python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\AmbientMood.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built dist\AmbientMood\AmbientMood.exe"

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "/DAppVersion=$Version" packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Built dist\AmbientMood-Setup-$Version.exe"
} else {
    Write-Warning "Inno Setup 6 not found (winget install JRSoftware.InnoSetup); only the dist\AmbientMood folder was built."
}
