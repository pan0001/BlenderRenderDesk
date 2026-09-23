"""Filesystem notifications and bounded, opt-in crash recovery."""
from pathlib import Path
import threading
import time
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from .engine.protocol import read, write


class Watchdog:
    def __init__(self, root):
        self.root = Path(root)
        self.options = {'files': True, 'recover': False, 'attempts': 2, 'delay': 30,
                        **read(self.root / 'watchdog.json', {})}
        self.history = read(self.root / 'recovery.json', {})
        self.changed = threading.Event()
        self.observer = Observer()
        owner = self
        class Events(FileSystemEventHandler):
            def on_any_event(self, event):
                if not event.is_directory and event.event_type in ('created', 'modified', 'moved', 'deleted'):
                    path = Path(getattr(event, 'dest_path', '') or event.src_path)
                    if path.name in ('progress.json', 'status.json') or path.suffix.lower() in ('.png', '.exr', '.jpg', '.jpeg', '.tiff', '.webp'):
                        owner.changed.set()
        self.handler = Events()
        self.watches = {}
        self.observer.start()
        self.error = ''

    def configure(self, values):
        attempts, delay = int(values.get('attempts', 2)), int(values.get('delay', 30))
        if not 1 <= attempts <= 5 or not 5 <= delay <= 3600:
            raise ValueError('恢复次数需为 1–5；恢复等待需为 5–3600 秒')
        self.options = {'files': bool(values.get('files')), 'recover': bool(values.get('recover')),
                        'attempts': attempts, 'delay': delay}
        write(self.root / 'watchdog.json', self.options)
        if not self.options['recover']:
            for row in self.history.values():
                row.pop('due', None)
            write(self.root / 'recovery.json', self.history)
        return self.settings()

    def settings(self):
        return {**self.options, 'watching': len(self.watches), 'error': self.error}

    def reset(self, jid):
        self.history.pop(jid, None)
        write(self.root / 'recovery.json', self.history)

    def cancel(self, jid):
        row = self.history.get(jid, {})
        row.pop('due', None)
        row['expected'] = False
        self.history[jid] = row
        write(self.root / 'recovery.json', self.history)

    def sync(self, jobs):
        paths = {self.root / 'jobs'} | {Path(j['output']) for j in jobs.values()}
        desired = {str(p.resolve()) for p in paths if p.is_dir()} if self.options['files'] else set()
        for path in self.watches.keys() - desired:
            self.observer.unschedule(self.watches.pop(path))
        for path in desired - self.watches.keys():
            try:
                self.watches[path] = self.observer.schedule(self.handler, path, recursive=Path(path) == self.root / 'jobs')
                self.error = ''
            except OSError as error:
                self.error = str(error)

    def observe(self, snapshot, controller):
        dirty = False
        now = time.time()
        for task in snapshot['tasks']:
            j, s = task['job'], task['status']
            row = self.history.setdefault(j['id'], {'attempts': 0})
            before = dict(row)
            if s.get('running'):
                row['expected'] = True
                row.pop('due', None)
            elif row.get('expected'):
                row['expected'] = False
                control = read(controller.directory(j['id']) / 'control.json', {})
                if s['state'] == 'interrupted' and not control.get('pause') and self.options['recover'] and row['attempts'] < self.options['attempts']:
                    row['due'] = now + self.options['delay']
                    controller.event(j['id'], 'Watchdog 检测到意外退出，等待后自动续渲染')
            if row.get('due') and not s.get('running') and self.options['recover']:
                if s['state'] != 'interrupted' or j.get('start_at') or j.get('pause_at'):
                    row.pop('due', None)
                elif now >= row['due']:
                    row.pop('due', None)
                    row['attempts'] += 1
                    try:
                        controller.start(j['id'])
                        row['expected'] = True
                        row.pop('error', None)
                    except Exception as error:
                        row['error'] = str(error)
                        controller.event(j['id'], 'Watchdog 恢复失败：' + str(error))
                        # Invalid source/receipts need attention, not a retry loop.
            task['status']['recovery'] = dict(row)
            dirty |= before != row
        if dirty:
            write(self.root / 'recovery.json', self.history)

    def close(self):
        self.observer.stop()
        self.observer.join(5)
