"""Entry point; same data-directory lock as Render Desk 1.x."""
import argparse
import os
from pathlib import Path
import sys
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTimer, Qt
from desktop import THEME, Window
from service import default_root


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=default_root())
    parser.add_argument('--smoke-test', action='store_true', help='Exit after the first service snapshot (for package validation)')
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName('BlenderRenderDesk')
    app.setStyle('Fusion')
    app.setStyleSheet(THEME)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.data_dir / 'manager.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if not lock.seek(0, 2):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        QMessageBox.information(None, 'Render Desk', '这个工作空间已有管理器运行，请先关闭旧窗口。后台 Blender 不会因此停止。')
        lock.close()
        return 1
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    app.setWindowIcon(QIcon(str(root / 'mark.svg')))
    window = Window(args.data_dir)
    if args.smoke_test:
        window.worker.snapshot_ready.connect(window.verify_startup)
        QTimer.singleShot(15000, window.close)
    window.show()
    result = app.exec()
    lock.close()
    return (0 if (args.data_dir / 'smoke-result.json').exists() else 2) if args.smoke_test else result


if __name__ == '__main__':
    raise SystemExit(main())
