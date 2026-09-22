# Third-party notices

The MIT license in LICENSE applies to Render Desk's original application code and artwork. It does not replace dependency licenses.

| Component | Version | License / source |
|---|---|---|
| PySide6 Essentials | 6.11.2 | LGPL-3.0-only option; https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| Shiboken6 | 6.11.2 | LGPL-3.0-only option; same Qt for Python source repository |
| Qt Core, Gui, Widgets and supporting runtime libraries | 6.11.2 | LGPL v3 option and component-specific third-party licenses; https://code.qt.io/cgit/qt/qtbase.git/ |
| psutil | 7.2.2 | BSD-3-Clause; https://github.com/giampaolo/psutil |
| Python | 3.13 | PSF License; https://www.python.org/downloads/source/ |
| PyInstaller (packaging tool / bootloader) | 6.22.3 | GPL with bootloader exception; https://pyinstaller.org/en/stable/license.html |

Blender is a separate user installation and is not bundled: https://www.blender.org/about/license/ .

The Windows build uses dynamically loaded Qt/PySide libraries in `_internal`. You may replace these libraries with compatible modified builds and rebuild the application from the complete supplied source. This distribution imposes no additional restriction on reverse engineering for debugging modifications to LGPL libraries. The app has no signature or activation mechanism preventing such replacement.

The `licenses` directory includes LGPL v3, GPL v3, psutil and Python license texts, package metadata, and the Qt project's collected third-party notices page. Qt also includes third-party works under their respective licenses; see the preserved notices and https://doc.qt.io/qt-6/licenses-used-in-qt.html .

Corresponding upstream source releases:

- Qt for Python 6.11.2: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/
- Qt 6.11.2: https://download.qt.io/official_releases/qt/6.11/6.11.2/single/
- psutil 7.2.2: https://pypi.org/project/psutil/7.2.2/#files

Build instructions are in README.md and build.ps1. Dependencies are not modified by this project. The UI test harness loads Windows fonts installed on the test machine; those fonts are not redistributed.
