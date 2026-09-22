import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import sys
import tempfile
import time
import faulthandler
faulthandler.dump_traceback_later(12)
from unittest.mock import patch
base = Path(tempfile.mkdtemp(prefix='renderdesk-ui-evidence-'))
(base/'work').mkdir()
(base/'outputs/BlenderRenderDesk-v2-source').mkdir(parents=True)
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from desktop import THEME, JobDialog, ScheduleDialog, Window
from service import Controller

app = QApplication([])
if os.name == 'nt':
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    QFontDatabase.addApplicationFont('C:/Windows/Fonts/consola.ttf')
app.setStyle('Fusion')
app.setStyleSheet(THEME)
root = Path(tempfile.mkdtemp(prefix='renderdesk-qt-'))
window = Window(root, connect_service=False)
window.show()
app.processEvents()
window.grab().save(str(base / 'outputs/BlenderRenderDesk-v2-source/preview.png'))
tasks = []
for index in range(35):
    tasks.append({'job': {'id': str(index), 'name': ['海边小屋 · 日落镜头', '角色动画 · 转身测试', '产品展示 · 玻璃材质'][index % 3], 'blend': f'D:/示例工程/scene_{index}.blend', 'output': 'D:/示例输出', 'start_at': None, 'pause_at': None},
                  'status': {'state': 'rendering' if index == 0 else 'paused' if index == 1 else 'ready', 'done': 48 if index == 0 else 12 if index == 1 else 0, 'total': 120, 'running': index == 0, 'pid': 1234 if index == 0 else None, 'elapsed': 620 if index == 0 else 190 if index == 1 else 0, 'eta': 930 if index == 0 else None, 'cpu': 13.4 if index == 0 else None, 'gpu': 92.0 if index == 0 else None, 'ram_mb': 4096 if index == 0 else None, 'vram_mb': 6200 if index == 0 else None}})
window.receive({'tasks': tasks, 'processes': [{'pid': 1234, 'created': time.time(), 'blend': 'D:/示例工程/scene_0.blend', 'args': ['blender.exe', '-b'], 'elapsed': 620}], 'system': {'cpu': 16.8, 'ram_used': 18.4, 'ram_total': 64}, 'notices': []})
window.table.selectRow(0)
app.processEvents()
assert window.start_button.isEnabled() is False and window.pause_button.isEnabled()
window.append_log(True, '界面示例数据 · 用于布局验证\n[RenderDesk] FRAME_SAVED 48\n' + '\n'.join(f'日志输出 {i}' for i in range(1600)))
assert window.logs.document().blockCount() <= 1200
assert window.table.verticalScrollBar().maximum() > 0
window.resize(1200, 780)
app.processEvents()
print('Layout sizes:', window.table.width(), window.logs.height(), flush=True)
assert window.table.width() > 700 and window.logs.height() > 70
window.grab().save(str(base / 'work/v2-ui-populated.png'))
dialog = JobDialog(window, root)
dialog.show()
app.processEvents()
assert dialog.fields['threads'].value() == 0
dialog.grab().save(str(base / 'work/v2-ui-form.png'))
dialog.close()
window.table.selectRow(1)
assert window.start_button.isEnabled() and not window.pause_button.isEnabled()
window.switch('processes')
window.table.selectRow(0)
assert not window.attach_button.isEnabled()  # already managed PID
window.close()
print('PASS UI: empty/populated layouts, custom scrollbars, task selection, bounded logs, managed PID identification', flush=True)

original = Controller.snapshot
def slow(self):
    time.sleep(.8)
    return original(self)

with patch.object(Controller, 'snapshot', slow):
    window = Window(root, connect_service=True)
    window.show()
    beats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    QTimer.singleShot(2600, window.close)
    QTimer.singleShot(6000, lambda: print('Watchdog', window.closing, window.stopped, window.service_thread.isRunning(), [(w.windowTitle(), w.isVisible()) for w in app.topLevelWidgets()], flush=True))
    app.exec()
    timer.stop()
    gaps = [b-a for a,b in zip(beats, beats[1:])]
    assert len(beats) > 100, len(beats)
    assert max(gaps) < .2, max(gaps)
    print(f'PASS UI responsiveness: {len(beats)} heartbeats; largest interval {max(gaps)*1000:.1f} ms with 800 ms backend delay', flush=True)

faulthandler.cancel_dump_traceback_later()
