param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$previousSearchPath = $env:PATH
Push-Location $PSScriptRoot
try {
    # Qt uses Windows ICU. Unrelated tools on PATH can provide an incompatible
    # icuuc.dll that a binary dependency scanner would otherwise bundle.
    $pythonDirectory = Split-Path (Get-Command $Python).Source
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$pythonDirectory"
    & $Python -m pip install -r requirements.txt 'pyinstaller==6.22.3'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $Python -m PyInstaller --noconfirm --windowed --onedir --name BlenderRenderDesk --icon app.ico --add-data 'runner.py;.' --add-data 'protocol.py;.' --add-data 'mark.svg;.' --exclude-module tkinter --exclude-module customtkinter app.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
    foreach ($file in @('README.md','LICENSE','THIRD_PARTY_NOTICES.md','PROVENANCE.md','preview.png')) {
        Copy-Item -LiteralPath $file -Destination 'dist/BlenderRenderDesk'
    }
    Copy-Item -LiteralPath 'licenses' -Destination 'dist/BlenderRenderDesk/licenses' -Recurse -Force
} finally {
    $env:PATH = $previousSearchPath
    Pop-Location
}
