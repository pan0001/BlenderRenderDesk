"""One controller thread shared by every desktop/browser client."""
from concurrent.futures import Future, ThreadPoolExecutor
import copy
from pathlib import Path
import queue
import threading
import time
from .service import Controller
from .projects import BlenderRegistry, scan_project, scene_values, public_scan, process_project
from .watch import Watchdog
from .previews import Previews
from .tunnel import Frpc
from .engine.protocol import signature
import uuid
from .notifications import Notifications
from .adoption import infer_script
from .batch import infer_batch


class AdoptionProgress:
    """Thread-safe phase information shared by coalesced adoption requests."""
    def __init__(self):
        self.lock = threading.Lock()
        self.value = {'step': 0, 'total': 4, 'message': '等待扫描线程',
                      'detail': '正在排队，原 Blender 继续渲染。'}

    def update(self, step, message, detail):
        with self.lock:
            self.value = dict(step=step, total=4, message=message, detail=detail)

    def snapshot(self):
        with self.lock:
            return dict(self.value)


class Runtime:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = BlenderRegistry(self.root)
        self.previews = Previews(self.root)
        self.tunnel = Frpc(self.root)
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='Project scan')
        self.scan_lock = threading.Lock()
        self.adopt_lock = threading.Lock()
        self.adopting = {}
        self.scans = {}
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
        if action == 'process.adopt':
            row = values['process']
            key = (row['pid'], row['created'])
            with self.adopt_lock:
                self.adopting = {k: f for k, f in self.adopting.items() if not f.done()}
                if key not in self.adopting:
                    progress = AdoptionProgress()
                    future = self.workers.submit(self._adopt, row, progress)
                    future.progress = progress
                    self.adopting[key] = future
                return self.adopting[key]
        if action == 'project.scan':
            return self.workers.submit(self._scan, values)
        if action == 'project.rescan':
            return self.workers.submit(self._rescan, values)
        if action == 'blender.save':
            return self.workers.submit(self.registry.update, values)
        if action.startswith('frpc.'):
            method = {'frpc.save': lambda: self.tunnel.configure(values), 'frpc.start': self.tunnel.start, 'frpc.stop': self.tunnel.stop}.get(action)
            if method:
                return self.workers.submit(method)
        future = Future()
        self.commands.put_nowait((action, values, future))
        return future

    def _adopt(self, row, progress=None):
        progress = progress or AdoptionProgress()
        progress.update(1, '检查进程与启动参数', '确认进程仍在运行，并检查原渲染脚本。')
        from .processes import process
        item = process(row)
        if not item:
            raise ValueError('进程已退出，请刷新列表')
        args, cwd = item.cmdline(), item.cwd()
        if not any(a in args for a in ('-b', '--background')) or args.count('--python') != 1 or any(a in args for a in ('--python-expr', '-a', '--render-anim', '-f', '--render-frame')):
            raise ValueError('此启动方式暂不能自动接管；支持单个 Python 脚本同步逐帧输出 PNG')
        script = (Path(cwd) / args[args.index('--python') + 1]).resolve()
        if not script.is_file():
            raise ValueError('原渲染脚本已不存在，无法识别接管配置')
        before = signature(script)
        if before['mtime_ns'] / 1e9 > row['created'] + 2:
            raise ValueError('原脚本在此进程启动后被修改，无法确认运行中的配置；等待该进程结束后再接管新进程')
        exe, path, _ = process_project(row)
        progress.update(2, '读取 Blender 工程', '正在后台扫描场景和工程配置。大工程可能需要较长时间，原渲染继续运行。')
        scan = scan_project(exe, path)
        progress.update(3, '识别脚本与输出配置', '解析本次帧范围、实际输出目录和文件名模板。')
        prepared = infer_script(script, args, cwd, scan)
        progress.update(3, '识别外层脚本与完整计划', '检查是否由 Python 脚本分批启动 Blender，并核对全部批次。')
        batch = infer_batch(item, scan)
        if batch:
            prepared['batch'] = batch
        if signature(script) != before:
            raise ValueError('读取工程期间脚本发生变化，请重试')
        prepared['process'] = {'pid': item.pid, 'created': row['created']}
        progress.update(4, '加入队列并核对已有帧', '保存任务配置，检查已完成帧；不会重启原渲染进程。')
        return self.submit('_adopt.apply', prepared).result(30)

    def _scan(self, values):
        row = values.get('process')
        if row:
            exe, path, args = process_project(row)
        else:
            exe, path = self.registry.resolve(values.get('blender_id')), values['path']
        scan = scan_project(exe, path)
        scan['scan_id'] = uuid.uuid4().hex
        if row:
            scan['process'] = row
        with self.scan_lock:
            if len(self.scans) >= 64:
                self.scans.pop(next(iter(self.scans)))
            self.scans[scan['scan_id']] = scan
        return public_scan(scan)

    def _rescan(self, values):
        job = next((t['job'] for t in self.snapshot()['tasks'] if t['job']['id'] == values['id']), None)
        if not job:
            raise ValueError('找不到任务')
        scan = scan_project(job['blender'], job['blend'])
        if job.get('auto_detected') and job.get('external'):
            scene = next(s for s in scan['scenes'] if s['scene'] == job['scene'])
            prepared = {'project': {k:v for k,v in scene.items() if k != 'project_paths'},
                        'source': scan['source'], 'start': job['start'], 'end': job['end'], 'step': job['step']}
            return self.submit('_rescan.apply', {'id': job['id'], 'prepared': prepared}).result(30)
        overrides = {'output': job['output']} if job.get('output_override') else {}
        prepared = scene_values(scan, job.get('scene'), overrides)
        return self.submit('_rescan.apply', {'id': job['id'], 'prepared': prepared}).result(30)

    def _import(self, values):
        with self.scan_lock:
            scan = self.scans.get(values['scan_id'])
        if not scan:
            raise ValueError('扫描结果已过期，请重新扫描')
        if signature(scan['blend']) != scan['source']:
            raise ValueError('工程在扫描后已变化，请重新扫描')
        prepared = scene_values(scan, values.get('scene'), values.get('overrides'))
        if scan.get('process'):
            prepared.update(process=scan['process'], output=values['output'], pattern=values['pattern'])
            # Only the selected process may be adopted; paths are revalidated by Controller.
            if prepared['format'] != 'PNG':
                raise ValueError('外部进程接入暂只支持 PNG 序列')
            return self.controller.attach(prepared)
        return self.controller.add_project(prepared)

    def _execute(self, action, values):
        c, n = self.controller, self.notifications
        if action == '_adopt.apply':
            from .engine.protocol import read
            if values.get('batch'):
                if signature(values['blend']) != values['source']:
                    raise ValueError('识别后工程发生变化，请重试')
                return c.attach_batch(values)
            row = values['process']
            for jid in c.jobs:
                identity = read(c.directory(jid) / 'launch.json', {})
                if identity.get('pid') == row['pid'] and identity.get('created') == row['created'] and c.live(jid):
                    return jid
            if signature(values['blend']) != values['source']:
                raise ValueError('识别后工程发生变化，请重试')
            return c.attach(values)
        if action == 'queue.edit':
            return c.edit(values['id'], values['changes'])
        if action == 'queue.reorder':
            return c.reorder(values['ids'])
        if action in ('queue.remove', 'queue.reset'):
            jid = values['id']
            result = c.request_change(jid, 'remove' if action == 'queue.remove' else 'reset', values.get('mode', 'rerender'))
            self.watchdog.reset(jid)
            return result
        if action == 'project.import':
            return self._import(values)
        if action == '_rescan.apply':
            return c.rescan(values['id'], values['prepared'])
        if action == 'watchdog.save':
            return self.watchdog.configure(values)
        if action == 'add':
            return c.add(values)
        if action == 'attach':
            return c.attach(values)
        if action in ('start', 'pause'):
            getattr(c, action)(values['id'])
            if action == 'start':
                self.watchdog.reset(values['id'])
            else:
                self.watchdog.cancel(values['id'])
        elif action == 'schedule':
            c.schedule(values['id'], values.get('start_at'), values.get('pause_at'))
        elif action == 'bark.save':
            return n.configure(values)
        elif action == 'bark.test':
            n.test()
        elif action == 'settings':
            return {'bark': n.settings(), 'blenders': self.registry.snapshot(), 'watchdog': self.watchdog.settings(), 'frpc': self.tunnel.settings()}
        elif action == 'refresh':
            pass
        elif action not in ('start', 'pause'):
            raise ValueError('未知操作')
        return True

    def _run(self):
        try:
            self.controller = Controller(self.root)
            self.notifications = Notifications(self.root)
            self.watchdog = Watchdog(self.root)
            # Observe completion before cleanup so a queued Bark notification is not lost.
            initial = []
            for i, j in self.controller.jobs.items():
                try:
                    if j.get('external', {}).get('phase') == 'attached' and not self.controller.live(i):
                        self.controller.observe_external(j)
                    initial.append({'job': j, 'status': self.controller.status(i)})
                except Exception:
                    pass  # Preserve invalid tasks for the normal error display.
            self.notifications.observe({'tasks': initial})
            self.controller.process_changes()
            self.controller.cleanup_completed()
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
                if time.monotonic() >= deadline or (self.watchdog.changed.is_set() and time.monotonic() >= deadline - 1.0):
                    self.watchdog.changed.clear()
                    self._refresh()
                    deadline = time.monotonic() + 1.5
        finally:
            self.watchdog.close()
            self.notifications.close()
            self.controller.close()

    def _refresh(self):
        try:
            snapshot = self.controller.snapshot()
            self.watchdog.sync(self.controller.jobs)
            self.watchdog.observe(snapshot, self.controller)
            self.notifications.observe(snapshot)
            snapshot['bark'] = self.notifications.settings()
            snapshot['watchdog'] = self.watchdog.settings()
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
        self.tunnel.close()
        self.workers.shutdown(wait=True, cancel_futures=True)
        self.stop.set()
        self.thread.join(15)
