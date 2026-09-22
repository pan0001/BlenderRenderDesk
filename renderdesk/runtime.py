"""One controller thread shared by every desktop/browser client."""
from concurrent.futures import Future
import copy
from pathlib import Path
import queue
import threading
import time
from .service import Controller
from .processes import installations
from .notifications import Notifications


class Runtime:
    def __init__(self, root):
        self.root = Path(root)
        self.commands = queue.Queue(maxsize=64)
        self.stop = threading.Event()
        self.ready = Future()
        self.lock = threading.Lock()
        self.cached = {'tasks': [], 'processes': [], 'system': {}, 'notices': []}
        self.thread = threading.Thread(target=self._run, name='Render controller', daemon=True)
        self.thread.start()
        self.ready.result(30)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.cached)

    def submit(self, action, values):
        future = Future()
        self.commands.put_nowait((action, values, future))
        return future

    def _execute(self, action, values):
        c, n = self.controller, self.notifications
        if action == 'add':
            return c.add(values)
        if action == 'attach':
            return c.attach(values)
        if action in ('start', 'pause'):
            getattr(c, action)(values['id'])
        elif action == 'schedule':
            c.schedule(values['id'], values.get('start_at'), values.get('pause_at'))
        elif action == 'bark.save':
            return n.configure(values)
        elif action == 'bark.test':
            n.test()
        elif action == 'settings':
            return {'bark': n.settings(), 'blenders': self.blenders}
        elif action == 'refresh':
            pass
        elif action not in ('start', 'pause'):
            raise ValueError('未知操作')
        return True

    def _run(self):
        try:
            self.controller = Controller(self.root)
            self.notifications = Notifications(self.root)
            self.blenders = installations()
            self._refresh()
            self.ready.set_result(True)
        except Exception as error:
            self.ready.set_exception(error)
            return
        deadline = time.monotonic() + 1.5
        try:
            while not self.stop.is_set():
                try:
                    action, values, future = self.commands.get(timeout=max(.01, min(.2, deadline - time.monotonic())))
                    try:
                        result = self._execute(action, values)
                        self._refresh()
                        future.set_result(result)
                    except Exception as error:
                        future.set_exception(error)
                except queue.Empty:
                    pass
                if time.monotonic() >= deadline:
                    self._refresh()
                    deadline = time.monotonic() + 1.5
        finally:
            self.notifications.close()
            self.controller.close()

    def _refresh(self):
        try:
            snapshot = self.controller.snapshot()
            self.notifications.observe(snapshot)
            snapshot['bark'] = self.notifications.settings()
            snapshot['updated'] = time.time()
            with self.lock:
                self.cached = snapshot
        except Exception as error:
            with self.lock:
                self.cached['notices'] = [str(error)]

    def log(self, jid, cursor=0):
        if jid not in {t['job']['id'] for t in self.snapshot()['tasks']}:
            raise ValueError('找不到任务')
        path = self.root / 'jobs' / jid / 'render.log'
        if not path.exists():
            return {'text': '', 'cursor': 0, 'reset': True}
        with path.open('rb') as stream:
            size = path.stat().st_size
            reset = cursor < 0 or cursor > size or size - cursor > 65536
            offset = max(0, size - 65536) if reset else cursor
            stream.seek(offset)
            block = stream.read(32768)
            # Leave a trailing partial UTF-8 character for the next request.
            import codecs
            decoder = codecs.getincrementaldecoder('utf8')('replace')
            text = decoder.decode(block)
            pending = len(decoder.getstate()[0])
            return {'text': text, 'cursor': stream.tell() - pending, 'reset': reset}

    def close(self):
        self.stop.set()
        self.thread.join(15)
