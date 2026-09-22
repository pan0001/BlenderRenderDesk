"""Render Desk 2 desktop: original Qt interface and queued background service."""
import time
from pathlib import Path
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QTextCursor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from processes import installations
from service import Controller, LABELS

VERSION = '2.0.0'
THEME = '''
QWidget { background: #10171d; color: #e1e9ed; font-family: "Microsoft YaHei UI"; font-size: 12px; }
QLabel { background: transparent; }
QMainWindow, QDialog { background: #10171d; }
QFrame#sidebar { background: #141e25; border-right: 1px solid #27333d; }
QFrame#card { background: #17222a; border: 1px solid #2a3944; border-radius: 12px; }
QFrame#card QLabel { background: transparent; }
QLabel#brand { font-size: 23px; font-weight: 700; color: #f1f7f9; }
QLabel#title { font-size: 26px; font-weight: 700; }
QLabel#muted { color: #8da2b1; background: transparent; }
QLabel#section { color: #77909f; font-size: 11px; font-weight: 600; }
QLabel#number { font-size: 24px; font-weight: 600; color: #d7eee9; }
QLabel#detailTitle { font-size: 17px; font-weight: 600; background: transparent; }
QPushButton { background: #202e39; border: 1px solid #344551; border-radius: 7px; padding: 9px 15px; }
QPushButton:hover { background: #2c404d; border-color: #6c8a99; }
QPushButton:pressed { background: #354c58; }
QPushButton:disabled { color: #586975; border-color: #26343e; background: #1a252d; }
QPushButton#primary { background: #63d8bc; color: #0b2622; border: 0; font-weight: 700; }
QPushButton#primary:hover { background: #8de9d2; }
QPushButton#primary:disabled { background: #284f48; color: #6a918a; }
QPushButton#nav { text-align: left; border: 0; background: transparent; padding: 13px 17px; color: #9dafbb; }
QPushButton#nav:checked { background: #243e40; color: #82e4ce; border-left: 3px solid #63d8bc; }
QPushButton#link { background: transparent; border: 0; color: #7fdac6; padding: 8px 0; text-align: left; }
QLineEdit, QSpinBox, QComboBox { background: #101b23; border: 1px solid #3b4d59; border-radius: 6px; padding: 8px; selection-background-color: #35665e; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #63d8bc; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: #202e39; selection-background-color: #345b55; }
QSpinBox::up-button, QSpinBox::down-button { width: 17px; border: 0; background: #263b47; }
QTableWidget { background: #131e26; alternate-background-color: #17232c; border: 1px solid #2a3944; border-radius: 9px; gridline-color: #263641; selection-background-color: #234c48; selection-color: #eafff9; }
QTableWidget::item { padding: 9px 8px; border-bottom: 1px solid #23313b; }
QHeaderView::section { background: #1d2b35; color: #94adbb; border: 0; border-bottom: 1px solid #344551; padding: 12px 8px; font-weight: 600; }
QTableCornerButton::section { background: #1d2b35; border: 0; }
QPlainTextEdit { background: #0d151c; border: 1px solid #293d49; border-radius: 7px; color: #b3c9d6; padding: 9px; font-family: Consolas; font-size: 12px; }
QProgressBar { background: #24363f; border: 0; border-radius: 4px; min-height: 7px; max-height: 7px; }
QProgressBar::chunk { background: #63d8bc; border-radius: 4px; }
QScrollBar:vertical { background: #15232c; width: 10px; margin: 0; border: 0; }
QScrollBar:horizontal { background: #15232c; height: 10px; margin: 0; border: 0; }
QScrollBar::handle:vertical { background: #46616f; min-height: 28px; border-radius: 5px; }
QScrollBar::handle:horizontal { background: #46616f; min-width: 28px; border-radius: 5px; }
QScrollBar::handle:hover { background: #75afad; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QSplitter::handle { background: #10171d; height: 10px; }
QCheckBox { background: transparent; spacing: 8px; color: #a9bcc8; }
QCheckBox::indicator { width: 15px; height: 15px; border: 1px solid #526b79; border-radius: 4px; background: #14212a; }
QCheckBox::indicator:checked { background: #63d8bc; border-color: #63d8bc; }
QToolTip { background: #283c49; color: #e6f4f9; border: 1px solid #668191; padding: 7px; }
'''


def text(value, name=None):
    label = QLabel(value)
    if name:
        label.setObjectName(name)
    return label


def button(label, callback, name=None):
    result = QPushButton(label)
    if name:
        result.setObjectName(name)
    result.clicked.connect(callback)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    return result


def duration(seconds):
    if seconds is None:
        return '—'
    value = max(0, int(seconds))
    return f'{value//3600:02}:{value//60%60:02}:{value%60:02}'


def metric(value, unit='%', digits=1):
    return '—' if value is None else f'{value:.{digits}f}{unit}'


def choose_file(parent, title, pattern):
    return QFileDialog.getOpenFileName(parent, title, '', pattern, options=QFileDialog.Option.DontUseNativeDialog)[0]


class ServiceThread(QObject):
    snapshot_ready = Signal(dict)
    log_ready = Signal(bool, str)
    failed = Signal(str)
    finished = Signal()
    created = Signal(str)

    def __init__(self, root):
        super().__init__()
        self.root, self.selected, self.controller = root, None, None

    @Slot()
    def begin(self):
        try:
            self.controller = Controller(self.root)
            self.timer = QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self.refresh)
            self.timer.start()
            self.refresh()
        except Exception as error:
            self.failed.emit(str(error))

    @Slot()
    def refresh(self):
        if not self.controller:
            return
        try:
            self.snapshot_ready.emit(self.controller.snapshot())
            if self.selected in self.controller.jobs:
                reset, chunk = self.controller.log_reader.poll(self.controller.directory(self.selected) / 'render.log')
                if reset or chunk:
                    self.log_ready.emit(reset, chunk)
        except Exception as error:
            self.failed.emit(str(error))

    @Slot(dict)
    def dispatch(self, request):
        action = request['action']
        if action == 'close':
            if hasattr(self, 'timer'):
                self.timer.stop()
            if self.controller:
                self.controller.close()
            self.finished.emit()
            return
        if not self.controller:
            self.failed.emit('任务服务尚未启动')
            return
        try:
            if action == 'select':
                self.selected = request.get('id')
            elif action in ('add', 'attach'):
                jid = getattr(self.controller, action)(request['values'])
                self.created.emit(jid)
            elif action == 'pause_all':
                for jid in self.controller.jobs:
                    if self.controller.live(jid):
                        self.controller.pause(jid)
            elif action in ('start', 'pause'):
                getattr(self.controller, action)(request['id'])
            elif action == 'schedule':
                self.controller.schedule(request['id'], **request['values'])
            elif action != 'refresh':
                raise ValueError('未知操作')
            self.refresh()
        except Exception as error:
            self.failed.emit(str(error))


class JobDialog(QDialog):
    def __init__(self, parent, root, process_row=None):
        super().__init__(parent)
        self.setWindowTitle('接入已有渲染' if process_row else '新建渲染任务')
        self.setMinimumWidth(620)
        self.process_row = process_row
        layout = QVBoxLayout(self)
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)
        layout.addWidget(text(self.windowTitle(), 'detailTitle'))
        description = text('设置输出与帧范围，Blender 将在后台完成渲染。' if not process_row else
            '首次暂停：检测下一张完整 PNG 后结束原进程，可能丢弃刚开始的下一帧。\n续渲染后采用协作式暂停。仅支持单个 --python 同步逐帧 PNG 脚本。', 'muted')
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        form.setSpacing(12)
        self.fields = {}

        def path_field(key, title, default, kind):
            entry = QLineEdit(default)
            row = QWidget()
            horizontal = QHBoxLayout(row)
            horizontal.setContentsMargins(0, 0, 0, 0)
            horizontal.addWidget(entry, 1)

            def pick():
                if kind == 'folder':
                    value = QFileDialog.getExistingDirectory(self, title, entry.text(), options=QFileDialog.Option.DontUseNativeDialog)
                else:
                    value = choose_file(self, title, kind)
                if value:
                    entry.setText(value)
            horizontal.addWidget(button('浏览', pick))
            self.fields[key] = entry
            form.addRow(title, row)

        if not process_row:
            paths = installations()
            path_field('blender', 'Blender 程序', paths[0] if paths else '', 'Blender (blender.exe);;全部文件 (*)')
            path_field('blend', '工程文件', '', 'Blender 工程 (*.blend)')
        path_field('output', '实际输出目录' if process_row else '输出根目录', str(Path(root) / 'renders') if not process_row else '', 'folder')
        ranges = QWidget()
        line = QHBoxLayout(ranges)
        line.setContentsMargins(0, 0, 0, 0)
        for key, label, default, low, high in [('start', '起始', 1, -1048574, 1048574), ('end', '结束', 250, -1048574, 1048574), ('step', '步长', 1, 1, 100000)]:
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setValue(default)
            self.fields[key] = spin
            line.addWidget(text(label, 'muted'))
            line.addWidget(spin)
        form.addRow('帧范围', ranges)
        if process_row:
            self.fields['pattern'] = QLineEdit('####.png')
            form.addRow('帧文件名模板', self.fields['pattern'])
            self.ack = QCheckBox('已确认目录、帧范围和文件名属于此进程，并理解首次暂停的行为')
        else:
            self.fields['format'] = QComboBox()
            self.fields['format'].addItems(['PNG', 'OPEN_EXR'])
            form.addRow('图像格式', self.fields['format'])
            self.fields['threads'] = QSpinBox()
            self.fields['threads'].setRange(0, 1024)
            self.fields['threads'].setSpecialValueText('自动')
            form.addRow('CPU 线程数', self.fields['threads'])
            self.fields['scene'] = QLineEdit()
            self.fields['scene'].setPlaceholderText('留空使用工程默认场景')
            form.addRow('场景名称', self.fields['scene'])
            self.fields['camera'] = QLineEdit()
            self.fields['camera'].setPlaceholderText('留空使用场景默认相机')
            form.addRow('相机名称', self.fields['camera'])
            self.ack = QCheckBox('允许此工程自动运行 Python（仅对可信工程启用）')
        layout.addLayout(form)
        layout.addWidget(self.ack)
        self.validation = text('', 'muted')
        self.validation.setWordWrap(True)
        layout.addWidget(self.validation)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        actions.button(QDialogButtonBox.StandardButton.Ok).setText('接入任务' if process_row else '创建任务')
        actions.button(QDialogButtonBox.StandardButton.Ok).setObjectName('primary')
        actions.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        actions.accepted.connect(self.validate_accept)
        actions.rejected.connect(self.reject)
        layout.addWidget(actions)

    def validate_accept(self):
        if self.process_row and not self.ack.isChecked():
            self.validation.setText('请先确认上面的首次暂停说明。')
            return
        if any(not self.fields[key].text().strip() for key in ('output',) if key in self.fields):
            self.validation.setText('请选择输出目录。')
            return
        self.accept()

    def values(self):
        data = {}
        for key, field in self.fields.items():
            data[key] = field.value() if isinstance(field, QSpinBox) else field.currentText() if isinstance(field, QComboBox) else field.text().strip()
        if self.process_row:
            data['process'] = self.process_row
        else:
            data['autoexec'] = self.ack.isChecked()
        return data


class ScheduleDialog(QDialog):
    def __init__(self, parent, job):
        super().__init__(parent)
        self.setWindowTitle('定时计划')
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.addWidget(text('安排渲染时间', 'detailTitle'))
        self.start_on, self.pause_on = QCheckBox('从现在起，多久后开始'), QCheckBox('从现在起，多久后按帧暂停')
        self.start_minutes, self.pause_minutes = QSpinBox(), QSpinBox()
        for spin, value in [(self.start_minutes, 30), (self.pause_minutes, 120)]:
            spin.setRange(1, 525600)
            spin.setValue(value)
            spin.setSuffix(' 分钟')
        form = QFormLayout()
        form.addRow(self.start_on, self.start_minutes)
        form.addRow(self.pause_on, self.pause_minutes)
        layout.addLayout(form)
        current = '\n'.join(label + time.strftime('%m-%d %H:%M', time.localtime(job[key])) for key, label in [('start_at', '已设开始：'), ('pause_at', '已设暂停：')] if job.get(key))
        note = text((current + '\n' if current else '') + '开始计划需要管理器保持打开。暂停会等待当前帧保存。\n两个选项都不勾选并保存，将清除现有计划。', 'muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Save).setText('保存计划')
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        layout.addWidget(controls)

    def values(self):
        now = time.time()
        return {'start_at': now + self.start_minutes.value() * 60 if self.start_on.isChecked() else None,
                'pause_at': now + self.pause_minutes.value() * 60 if self.pause_on.isChecked() else None}


class Window(QMainWindow):
    command = Signal(dict)

    def __init__(self, root, connect_service=True):
        super().__init__()
        self.root = Path(root)
        self.mode, self.selected, self.closing, self.stopped = 'tasks', None, False, False
        self.data = {'tasks': [], 'processes': [], 'system': {}}
        self.pending_id = None
        self.setWindowTitle(f'Blender Render Desk · {VERSION}')
        self.resize(1400, 940)
        self.setMinimumSize(1100, 800)
        body = QWidget()
        shell = QHBoxLayout(body)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(body)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(216)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 30, 22, 20)
        side.setSpacing(12)
        side.addWidget(text('R / D', 'number'))
        side.addWidget(text('Render Desk', 'brand'))
        side.addWidget(text('BLENDER / BACKGROUND', 'section'))
        side.addSpacing(35)
        side.addWidget(text('工作空间', 'section'))
        self.task_nav = button('渲染任务', lambda: self.switch('tasks'), 'nav')
        self.proc_nav = button('本机进程', lambda: self.switch('processes'), 'nav')
        for nav in (self.task_nav, self.proc_nav):
            nav.setCheckable(True)
            side.addWidget(nav)
        self.task_nav.setChecked(True)
        side.addStretch()
        side.addWidget(text('随时暂停，随时回来。', 'muted'))
        side.addWidget(text('任务与进度自动保存', 'section'))
        side.addSpacing(16)
        side.addWidget(button('B站：@in_uni  ↗', lambda: QDesktopServices.openUrl(QUrl('https://space.bilibili.com/1856651886/')), 'link'))
        side.addWidget(text(f'OPEN SOURCE   /   {VERSION}', 'section'))
        shell.addWidget(sidebar)
        main = QWidget()
        content = QVBoxLayout(main)
        content.setContentsMargins(28, 27, 28, 20)
        content.setSpacing(17)
        heading = QHBoxLayout()
        titles = QVBoxLayout()
        self.title = text('渲染工作台', 'title')
        self.subtitle = text('让 Blender 在后台工作，把时间留给自己。', 'muted')
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        heading.addLayout(titles, 1)
        heading.addWidget(button('刷新列表', lambda: self.send('refresh')))
        heading.addWidget(button('＋ 新建任务', self.add_job, 'primary'))
        content.addLayout(heading)
        statistics = QHBoxLayout()
        statistics.setSpacing(12)
        self.stats = []
        for title in ('正在渲染', '已保存帧', '系统 CPU', '系统内存'):
            card = QFrame()
            card.setObjectName('card')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(17, 13, 17, 13)
            card_layout.addWidget(text(title, 'muted'))
            number = text('—', 'number')
            self.stats.append(number)
            card_layout.addWidget(number)
            statistics.addWidget(card)
        content.addLayout(statistics)
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索工程名称、路径或 PID…')
        self.search.textChanged.connect(self.render_table)
        toolbar.addWidget(self.search, 1)
        self.start_button = button('开始 / 继续', lambda: self.action('start'), 'primary')
        self.pause_button = button('Ⅱ 按帧暂停', lambda: self.action('pause'))
        self.plan_button = button('定时计划', self.schedule)
        self.attach_button = button('接入此进程', self.attach)
        for control in (self.start_button, self.pause_button, self.plan_button, self.attach_button):
            toolbar.addWidget(control)
        self.attach_button.hide()
        content.addLayout(toolbar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 8)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(53)
        self.table.horizontalHeader().setMinimumSectionSize(75)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        split.addWidget(self.table)
        details = QFrame()
        details.setObjectName('card')
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(19, 17, 19, 16)
        detail_layout.setSpacing(11)
        detail_heading = QHBoxLayout()
        self.detail_title = text('准备好下一次渲染', 'detailTitle')
        detail_heading.addWidget(self.detail_title, 1)
        self.open_output = button('输出目录 ↗', lambda: self.open_path('output'), 'link')
        self.open_log = button('完整日志 ↗', lambda: self.open_path('log'), 'link')
        detail_heading.addWidget(self.open_output)
        detail_heading.addWidget(self.open_log)
        detail_layout.addLayout(detail_heading)
        self.detail_info = text('新建任务，或从本机进程中接入已有渲染。', 'muted')
        self.detail_info.setWordWrap(True)
        self.detail_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.detail_info)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        detail_layout.addWidget(self.progress)
        log_heading = QHBoxLayout()
        log_heading.addWidget(text('运行日志', 'section'))
        log_heading.addStretch()
        self.follow = QCheckBox('跟随最新日志')
        self.follow.setChecked(True)
        log_heading.addWidget(self.follow)
        detail_layout.addLayout(log_heading)
        self.logs = QPlainTextEdit()
        self.logs.setMinimumHeight(115)
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1200)
        self.logs.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.logs.setPlaceholderText('选择任务查看实时输出。原进程的历史日志需要其启动时已保存到文件。')
        detail_layout.addWidget(self.logs, 1)
        split.addWidget(details)
        split.setSizes([320, 310])
        split.setChildrenCollapsible(False)
        content.addWidget(split, 1)
        bottom = QHBoxLayout()
        self.notice = text('●  本地工作空间 · 正在连接任务服务', 'muted')
        self.notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self.notice, 1)
        bottom.addWidget(button('暂停全部', lambda: self.send('pause_all'), 'link'))
        content.addLayout(bottom)
        shell.addWidget(main, 1)
        self.render_table()
        self.update_details()
        if connect_service:
            self.service_thread = QThread(self)
            self.worker = ServiceThread(root)
            self.worker.moveToThread(self.service_thread)
            self.service_thread.started.connect(self.worker.begin)
            self.command.connect(self.worker.dispatch)
            self.worker.snapshot_ready.connect(self.receive)
            self.worker.log_ready.connect(self.append_log)
            self.worker.failed.connect(self.error)
            self.worker.created.connect(self.job_created)
            self.worker.finished.connect(self.service_thread.quit)
            self.worker.finished.connect(self.worker.deleteLater)
            self.service_thread.finished.connect(self.service_stopped)
            self.service_thread.start()

    def send(self, action, **values):
        self.command.emit({'action': action, **values})

    @Slot(str)
    def error(self, message):
        self.notice.setText('●  ' + message[:180])
        self.notice.setToolTip(message)
        self.notice.setStyleSheet('color: #f3b777')

    @Slot(str)
    def job_created(self, jid):
        self.pending_id = jid
        self.switch('tasks')

    def switch(self, mode):
        if mode != self.mode:
            self.selected = None
            self.logs.clear()
            self.send('select', id=None)
        self.mode = mode
        self.task_nav.setChecked(mode == 'tasks')
        self.proc_nav.setChecked(mode == 'processes')
        self.title.setText('渲染工作台' if mode == 'tasks' else '本机 Blender 进程')
        self.attach_button.setVisible(mode == 'processes')
        for control in (self.start_button, self.pause_button, self.plan_button):
            control.setVisible(mode == 'tasks')
        self.render_table()
        self.update_details()

    @Slot(dict)
    def receive(self, snapshot):
        self.data = snapshot
        self.stats[0].setText(str(len(snapshot['processes'])))
        self.stats[1].setText(str(sum(t['status'].get('done', 0) for t in snapshot['tasks'])))
        system = snapshot['system']
        self.stats[2].setText(metric(system.get('cpu')))
        self.stats[3].setText(f"{system.get('ram_used', 0):.1f} / {system.get('ram_total', 0):.0f} GB")
        if snapshot.get('notices'):
            self.error(snapshot['notices'][-1])
        else:
            self.notice.setText(f'●  已连接 · {len(snapshot["tasks"])} 个任务 · 关闭窗口后已启动的渲染会继续')
        self.render_table()
        self.update_details()

    def render_table(self):
        tasks = self.mode == 'tasks'
        headers = ['工程 / 任务', '状态', '已保存', '累计运行', 'CPU', '内存', 'GPU¹', '显存'] if tasks else ['工程 / PID', '启动方式', '进度', '运行时间', 'CPU', '内存', 'GPU¹', '显存']
        self.table.setHorizontalHeaderLabels(headers)
        term = self.search.text().strip().lower()
        rows = []
        if tasks:
            for item in self.data['tasks']:
                job, status = item['job'], item['status']
                if term and term not in (job['name'] + job['blend'] + str(status.get('pid', ''))).lower():
                    continue
                progress = f"{status.get('done', 0)} / {status.get('total', 0)}"
                rows.append((job['id'], [job['name'], LABELS.get(status['state'], status['state']), progress, duration(status.get('elapsed')),
                    metric(status.get('cpu')), metric(status.get('ram_mb'), ' MB', 0), metric(status.get('gpu')), metric(status.get('vram_mb'), ' MB', 0)], job['blend']))
        else:
            managed = {t['status'].get('pid'): t for t in self.data['tasks'] if t['status'].get('pid')}
            for process_row in self.data['processes']:
                name = Path(process_row['blend']).stem if process_row['blend'] else 'Blender'
                name += f"  ·  {process_row['pid']}"
                if term and term not in (name + process_row['blend']).lower():
                    continue
                task = managed.get(process_row['pid'])
                progress = f"{task['status']['done']} / {task['status']['total']}" if task else '未接入 / 未知'
                background = any(a in process_row['args'] for a in ('-b', '--background'))
                rows.append((str(process_row['pid']), [name, '已管理' if task else '后台脚本' if background else 'Blender 界面', progress,
                    duration(process_row.get('elapsed')), metric(process_row.get('cpu')), metric(process_row.get('ram_mb'), ' MB', 0),
                    metric(process_row.get('gpu')), metric(process_row.get('vram_mb'), ' MB', 0)], ' '.join(process_row['args'])))
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        selected_row = -1
        for index, (key, values, tooltip) in enumerate(rows):
            if key == (self.pending_id or self.selected):
                selected_row = index
            for column, value in enumerate(values):
                cell = self.table.item(index, column)
                if not cell:
                    cell = QTableWidgetItem()
                    self.table.setItem(index, column, cell)
                if cell.text() != value:
                    cell.setText(value)
                cell.setData(Qt.ItemDataRole.UserRole, key)
                cell.setToolTip(tooltip if column == 0 else 'GPU¹：该进程最忙 GPU 引擎的利用率；不可用显示 —' if column == 6 else value)
                if column == 1:
                    cell.setForeground(QColor('#74dcc2' if '渲染' in value or '管理' in value else '#a9bdca'))
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate([0, 165, 95, 110, 75, 90, 75, 90]):
            if column:
                self.table.setColumnWidth(column, width)
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.clearSelection()
        self.table.blockSignals(False)
        if self.pending_id and selected_row >= 0:
            self.pending_id = None
            self.selection_changed()

    def selection_changed(self):
        row = self.table.currentRow()
        cell = self.table.item(row, 0) if row >= 0 else None
        key = cell.data(Qt.ItemDataRole.UserRole) if cell and self.table.selectedItems() else None
        if key != self.selected:
            self.selected = key
            self.logs.clear()
            jid = self.current_task()
            self.send('select', id=jid['job']['id'] if jid else None)
        self.update_details()

    def current_task(self):
        if self.mode == 'tasks':
            return next((t for t in self.data['tasks'] if t['job']['id'] == self.selected), None)
        return next((t for t in self.data['tasks'] if t['status'].get('pid') and str(t['status']['pid']) == self.selected), None)

    def update_details(self):
        task = self.current_task()
        live = bool(task and task['status'].get('running'))
        self.start_button.setEnabled(bool(task) and not live)
        self.pause_button.setEnabled(live)
        self.plan_button.setEnabled(bool(task))
        self.open_output.setEnabled(bool(task))
        self.open_log.setEnabled(bool(task))
        self.attach_button.setEnabled(bool(self.selected) and not task)
        if task:
            job, state = task['job'], task['status']
            self.detail_title.setText(job['name'])
            plan = '   '.join(label + time.strftime('%m-%d %H:%M', time.localtime(job[key])) for key, label in [('start_at', '开始 '), ('pause_at', '暂停 ')] if job.get(key))
            current = f"   ·   当前帧 {state['frame']}" if live and state.get('frame') is not None else ''
            self.detail_info.setText(f"{LABELS.get(state['state'], state['state'])}{current}   ·   已保存 {state.get('done', 0)} / {state.get('total', 0)} 帧   ·   运行 {duration(state.get('elapsed'))}   ·   预计剩余 {duration(state.get('eta'))}\n" + (state.get('message') or plan or job['output']))
            self.progress.setValue(round(state.get('done', 0) / max(1, state.get('total', 1)) * 1000))
        elif self.mode == 'processes' and self.selected:
            row = next((r for r in self.data['processes'] if str(r['pid']) == self.selected), None)
            self.detail_title.setText('外部 Blender 进程')
            self.detail_info.setText('未接入的进程可查看资源与运行时间。需要确认输出目录和帧范围后，才能按完整 PNG 监测进度。\n' + (row.get('blend', '') if row else '进程已退出'))
            self.progress.setValue(0)
        else:
            self.detail_title.setText('从一个渲染任务开始')
            self.detail_info.setText('添加 .blend 工程，安排时间；需要用电脑时，保存当前帧后暂停。')
            self.progress.setValue(0)

    @Slot(bool, str)
    def append_log(self, reset, chunk):
        if reset:
            self.logs.clear()
        bar = self.logs.verticalScrollBar()
        position = bar.value()
        cursor = QTextCursor(self.logs.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)
        # Also bound a single extremely long line.
        if self.logs.document().characterCount() > 160000:
            trim = QTextCursor(self.logs.document())
            trim.setPosition(0)
            trim.setPosition(self.logs.document().characterCount() - 120000, QTextCursor.MoveMode.KeepAnchor)
            trim.removeSelectedText()
        bar.setValue(bar.maximum() if self.follow.isChecked() else position)

    def add_job(self):
        dialog = JobDialog(self, self.root)
        if dialog.exec():
            self.send('add', values=dialog.values())

    def attach(self):
        row = next((r for r in self.data['processes'] if str(r['pid']) == self.selected), None)
        if row:
            dialog = JobDialog(self, self.root, row)
            if dialog.exec():
                self.send('attach', values=dialog.values())

    def action(self, action):
        task = self.current_task()
        if task:
            self.send(action, id=task['job']['id'])

    def schedule(self):
        task = self.current_task()
        if task:
            dialog = ScheduleDialog(self, task['job'])
            if dialog.exec():
                self.send('schedule', id=task['job']['id'], values=dialog.values())

    def open_path(self, kind):
        task = self.current_task()
        if task:
            path = Path(task['job']['output']) if kind == 'output' else self.root / 'jobs' / task['job']['id'] / 'render.log'
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    @Slot()
    def service_stopped(self):
        self.stopped = True
        self.close()

    @Slot(dict)
    def verify_startup(self, snapshot):
        from protocol import write
        write(self.root / 'smoke-result.json', {'ok': True, 'tasks': len(snapshot['tasks'])})
        QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if hasattr(self, 'service_thread') and not self.stopped:
            event.ignore()
            if not self.closing:
                self.closing = True
                self.setEnabled(False)
                self.send('close')
        else:
            event.accept()
