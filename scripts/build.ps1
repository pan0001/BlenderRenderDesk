param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    & $Python -m pip install -r requirements.txt 'pyinstaller==6.22.3'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $Python -m PyInstaller --noconfirm --windowed --onedir --name BlenderRenderDesk --icon assets/app.ico --add-data 'renderdesk/web;renderdesk/web' --add-data 'renderdesk/engine;renderdesk/engine' --add-data 'vendor;vendor' --collect-all webview --exclude-module PySide6 --exclude-module PyQt6 --exclude-module PyQt5 --exclude-module tkinter app.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    foreach ($file in @('README.md','VERSION','LICENSE','docs','licenses')) {
        Copy-Item -LiteralPath $file -Destination 'dist/BlenderRenderDesk' -Recurse -Force
    }
} finally { Pop-Location }
