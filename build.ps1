# Builds dist\ExeLat.exe (single file, no console window).
# Prerequisites: Python 3.10+, and Node.js only if you changed index.html classes.
#   python -m venv .venv
#   .\.venv\Scripts\pip install -r requirements.txt pyinstaller
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (Get-Command npm -ErrorAction SilentlyContinue) {
    if (-not (Test-Path node_modules)) { npm install --no-audit --no-fund }
    npm run css
}

& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name ExeLat `
    --icon assets\icon.ico `
    --add-data "index.html;." `
    --add-data "assets\app.css;assets" `
    app.py

Get-Item dist\ExeLat.exe | Select-Object Name, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
