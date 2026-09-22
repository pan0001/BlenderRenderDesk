$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m PyInstaller --noconfirm --clean --windowed --onedir --name BlenderRenderDesk --collect-all customtkinter --collect-all darkdetect --icon app.ico --add-data 'app.ico;.' --add-data 'blender_worker.py;.' --add-data 'external_bridge.py;.' --add-data 'external_utils.py;.' app.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
Copy-Item -LiteralPath README.md -Destination dist\BlenderRenderDesk\使用说明.md
Copy-Item -LiteralPath CHANGELOG.md -Destination dist\BlenderRenderDesk\CHANGELOG.md
Compress-Archive -LiteralPath dist\BlenderRenderDesk -DestinationPath dist\BlenderRenderDesk-Windows.zip -Force
