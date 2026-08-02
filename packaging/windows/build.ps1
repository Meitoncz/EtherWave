<#
.SYNOPSIS
Builds "EtherWave Server.exe": generates the icon, converts it to .ico, and
runs PyInstaller against EtherWaveServer.spec. Run from anywhere; paths are
resolved relative to this script's location.

Requirements: Windows, Python 3.10+.
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$AssetsDir = Join-Path $ProjectRoot "assets"

# Use the venv interpreter if one exists at the project root (this repo's
# established local-dev convention -- see CLAUDE.md); fall back to whatever
# `python` resolves to on PATH (e.g. inside a CI runner's own venv).
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

Write-Host "==> Installing build dependencies"
& $PythonExe -m pip install --quiet --upgrade pip
& $PythonExe -m pip install --quiet -r (Join-Path $ProjectRoot "requirements.txt") `
    -r (Join-Path $ProjectRoot "requirements-windows.txt") pyinstaller Pillow

Write-Host "==> Generating icon.png"
& $PythonExe (Join-Path $AssetsDir "generate_icon.py")

Write-Host "==> Converting icon.png to icon.ico"
& $PythonExe -c @"
from PIL import Image
img = Image.open(r'$AssetsDir\icon.png')
img.save(r'$AssetsDir\icon.ico', sizes=[(16,16),(32,32),(48,48),(128,128),(256,256)])
"@

Write-Host "==> Running PyInstaller"
Push-Location $ScriptDir
try {
    & $PythonExe -m PyInstaller EtherWaveServer.spec --noconfirm `
        --distpath (Join-Path $ScriptDir "dist") --workpath (Join-Path $ScriptDir "build")
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "==> Done: $ScriptDir\dist\EtherWave Server"
Write-Host "    Run '$ScriptDir\dist\EtherWave Server\EtherWave Server.exe' to try it, or zip the"
Write-Host "    'EtherWave Server' folder for distribution."
