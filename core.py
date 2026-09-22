"""Render Desk's durable local jobs. No Blender UI/add-on required."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import threading
from pathlib import Path
import psutil

ACTIVE = {'loading', 'rendering', 'starting', 'pausing'}
LABELS = {'ready': '待开始', 'loading': '加载工程', 'rendering': '渲染中',
          'starting': '启动中', 'pausing': '完成当前帧后退出', 'paused': '已暂停 · 进程已退出',
          'complete': '已完成', 'error': '失败 · 查看日志', 'interrupted': '已中断 · 可续渲染',
          'finishing': '正在退出 Blender'}
LABELS['watching'] = '等待新帧落盘后结束'

class ResourceMonitor:
    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    def sample(self, pid, created):
        if created is None:
            return {}
        with self.lock:
            key = (pid, created)
            try:
                now = time.monotonic()
                if key not in self.cache:
                    p = psutil.Process(pid)
                    if abs(p.create_time()-created) > .01:
                        return {}
                    p.cpu_percent()
                    self.cache[key] = [p, 0, {}]
                item = self.cache[key]
                p = item[0]
                if not p.is_running():
                    self.cache.pop(key, None)
                    return {}
                if now-item[1] >= .5:
                    item[2] = {'cpu':round(p.cpu_percent()/max(1,psutil.cpu_count() or 1),1),
                               'ram_mb':round(p.memory_info().rss/1024**2),
                               'elapsed':max(0,int(time.time()-created))}
                    item[1] = now
                return item[2]
            except psutil.Error:
                self.cache.pop(key,None)
                return {}

RESOURCE_MONITOR = ResourceMonitor()

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {} if default is None else default

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.03)
    finally:
        tmp.unlink(missing_ok=True)

def fingerprint(path):
    s = Path(path).stat()
    return {'size': s.st_size, 'mtime_ns': s.st_mtime_ns}

def default_data_dir():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'BlenderRenderDesk'

def resource(name):
    return Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / name

def alive(launch):
    try:
        p = psutil.Process(int(launch['pid']))
        return p.is_running() and abs(p.create_time() - launch['created']) < .01
    except (psutil.Error, KeyError, ValueError):
        return False

def detect_blender():
    found = []
    for p in psutil.process_iter(['name']):
        if (p.info['name'] or '').lower() in ('blender.exe', 'blender'):
            try:
                exe = p.exe()
                if exe: found.append(exe)
            except psutil.Error:
                pass
    base = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Blender Foundation'
    if base.exists():
        found += [str(p) for p in sorted(base.glob('Blender */blender.exe'), reverse=True)]
    if shutil.which('blender'):
        found.append(shutil.which('blender'))
    return list(dict.fromkeys(found))

def scan_processes():
    rows = []
    for p in psutil.process_iter(['name']):
        if (p.info['name'] or '').lower() not in ('blender.exe', 'blender'):
            continue
        try:
            i = p.as_dict(['pid', 'exe', 'cmdline', 'create_time', 'memory_info'])
        except psutil.Error:
            continue
        args = i['cmdline'] or []
        blends = [a for a in args if a.lower().endswith('.blend')]
        rows.append({'pid': i['pid'], 'exe': i['exe'] or '', 'args': args,
                     'created': i['create_time'], 'blend': blends[0] if blends else '',
                     'memory': round(i['memory_info'].rss / 1024**2) if i['memory_info'] else None,
                     **RESOURCE_MONITOR.sample(i['pid'],i['create_time'])})
    return rows

class Manager:
    def __init__(self, root=None):
        self.root = Path(root or default_data_dir())
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.errors = []
        self.children = {}
        self.external_cache = {}
        for p in self.root.glob('jobs/*/job.json'):
            try:
                j = read_json(p)
                self.jobs[j['id']] = j
            except (ValueError, KeyError) as e:
                self.errors.append(f'{p}: {e}')

    def directory(self, job_id):
        return self.root / 'jobs' / job_id

    def save(self, job):
        atomic_json(self.directory(job['id']) / 'job.json', job)

    def add(self, blend, blender, start, end, step, output, image_format='PNG', threads=0, autoexec=False):
        blend, blender = Path(blend).resolve(), Path(blender).resolve()
        if not blend.is_file() or blend.suffix.lower() != '.blend':
            raise ValueError('请选择已保存的 .blend 文件。')
        if not blender.is_file():
            raise ValueError('请选择有效的 Blender 可执行文件。')
        if not (-1048574 <= start <= end <= 1048574) or step < 1:
            raise ValueError('帧范围无效：结束帧需不小于起始帧，步长至少为 1。')
        if threads < 0 or threads > 1024:
            raise ValueError('CPU 线程数应在 0–1024 之间。')
        if image_format not in ('PNG', 'OPEN_EXR'):
            raise ValueError('请选择 PNG 或 OPEN_EXR。')
        job_id = uuid.uuid4().hex[:12]
        # Every job has its own output directory; never overwrite another job's frames.
        out = Path(output).resolve() / f'{blend.stem}_{job_id}'
        out.mkdir(parents=True, exist_ok=False)
        job = {'id': job_id, 'name': blend.stem, 'blend': str(blend), 'blender': str(blender),
               'start': start, 'end': end, 'step': step, 'output': str(out), 'format': image_format,
               'threads': threads, 'autoexec': autoexec, 'source': fingerprint(blend),
               'created_at': time.time(), 'start_at': None, 'pause_at': None}
        self.save(job)
        self.jobs[job_id] = job
        return job

    def live(self, job_id):
        child = self.children.get(job_id)
        if child is not None and child.poll() is not None:
            del self.children[job_id]
        return alive(read_json(self.directory(job_id) / 'launch.json'))

    def status(self, job_id):
        d = self.directory(job_id)
        status = read_json(d / 'status.json', {'state': 'ready'})
        progress = read_json(d / 'progress.json', {'done': {}})
        running = self.live(job_id)
        state = status['state']
        if running:
            if state in ('complete', 'error', 'paused'):
                state = 'finishing'
            elif read_json(d / 'control.json').get('pause'):
                state = 'watching' if self.jobs[job_id].get('external',{}).get('phase') == 'attached' else 'pausing'
        elif state in ACTIVE:
            state = 'interrupted'
        launch = read_json(d / 'launch.json')
        metrics = RESOURCE_MONITOR.sample(launch['pid'],launch['created']) if running else {}
        if not running and launch.get('created') and status.get('updated'):
            metrics['elapsed'] = max(0, int(status['updated']-launch['created']))
        return {**status, 'state': state, 'running': running, 'done': len(progress['done']),
                'pid':launch.get('pid') if running else None, **metrics}

    def log(self, job_id, message):
        with (self.directory(job_id) / 'render.log').open('a', encoding='utf-8') as f:
            f.write(f'\n[Render Desk {time.strftime("%Y-%m-%d %H:%M:%S")}] {message}\n')

    def start(self, job_id):
        j, d = self.jobs[job_id], self.directory(job_id)
        if self.live(job_id):
            raise ValueError('此任务的 Blender 仍在运行，请等待退出。')
        if fingerprint(j['blend']) != j['source']:
            raise ValueError('原 .blend 文件自创建任务后已被修改。为避免混用不同版本的帧，请新建任务。')
        if not Path(j['blender']).is_file():
            raise ValueError('找不到此任务使用的 Blender，请恢复安装路径。')
        if j.get('external'):
            import external
            return external.start(self,j)
        progress = read_json(d / 'progress.json', {'done': {}})
        valid = {}
        for frame, record in progress['done'].items():
            p = Path(record['path'])
            if p.exists() and p.stat().st_size == record['size'] and record['size'] > 0:
                valid[frame] = record
            elif p.exists():
                raise ValueError(f'已完成帧被修改，停止续渲染以免覆盖：{p}')
        atomic_json(d / 'progress.json', {'done': valid})
        count = len(range(j['start'], j['end'] + 1, j['step']))
        if len(valid) == count:
            raise ValueError('此任务的全部帧均已完成。')
        # Keep a per-job runner, so a manager update cannot remove an active worker's script.
        shutil.copyfile(resource('blender_worker.py'), d / 'blender_worker.py')
        atomic_json(d / 'control.json', {'pause': False})
        atomic_json(d / 'status.json', {'state': 'starting', 'updated': time.time()})
        j['start_at'] = None
        self.save(j)
        command = [j['blender'], '--background', '--enable-autoexec' if j['autoexec'] else '--disable-autoexec',
                   j['blend'], '--python-exit-code', '23']
        if j['threads']:
            command += ['--threads', str(j['threads'])]
        command += ['--python', str(d / 'blender_worker.py'), '--', str(d)]
        self.log(job_id, '启动 / 续渲染：' + subprocess.list2cmdline(command))
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        try:
            with (d / 'render.log').open('ab', buffering=0) as log:
                child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, cwd=str(Path(j['blend']).parent),
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=env)
            self.children[job_id] = child
            created = psutil.Process(child.pid).create_time()
            atomic_json(d / 'launch.json', {'pid': child.pid, 'created': created})
        except Exception as e:
            atomic_json(d / 'status.json', {'state': 'error', 'message': str(e)})
            raise

    def pause(self, job_id):
        if not self.live(job_id):
            raise ValueError('任务当前没有运行。')
        if self.jobs[job_id].get('external',{}).get('phase') == 'attached':
            import external
            external.pause(self,self.jobs[job_id])
        else:
            atomic_json(self.directory(job_id) / 'control.json', {'pause': True})
            self.log(job_id, '请求暂停：当前帧保存后退出 Blender。')
        # An explicit pause must not be undone by an old scheduled start.
        j = self.jobs[job_id]
        j['start_at'] = None
        j['pause_at'] = None
        self.save(j)

    def schedule(self, job_id, start_at, pause_at):
        now = time.time()
        if start_at is not None and start_at <= now:
            raise ValueError('开始时间必须在未来。')
        if pause_at is not None and pause_at <= now:
            raise ValueError('暂停时间必须在未来。')
        if start_at and pause_at and pause_at <= start_at:
            raise ValueError('暂停时间必须晚于开始时间。')
        j = self.jobs[job_id]
        j.update(start_at=start_at, pause_at=pause_at)
        self.save(j)

    def tick(self, now=None):
        now = time.time() if now is None else now
        notices = []
        for j in list(self.jobs.values()):
            jid = j['id']
            if j.get('external',{}).get('phase') == 'attached':
                try:
                    import external
                    external.refresh(self,j)
                except Exception as e:
                    notices.append(f"{j['name']}：外部进程监测失败：{e}")
            # If both times passed while closed/asleep, don't start an expired window.
            if j['pause_at'] and now >= j['pause_at']:
                j.update(start_at=None, pause_at=None)
                self.save(j)
                try:
                    if self.live(jid):
                        self.pause(jid)
                        msg = '定时暂停已请求，等待当前帧保存。'
                    else:
                        msg = '暂停时刻已到；任务未运行，已取消过期计划。'
                    self.log(jid, msg)
                    notices.append(f"{j['name']}：{msg}")
                except Exception as e:
                    self.log(jid, f'定时暂停失败：{e}')
                    notices.append(str(e))
            elif j['start_at'] and now >= j['start_at']:
                j['start_at'] = None
                self.save(j)
                try:
                    if not self.live(jid):
                        self.start(jid)
                    notices.append(f"{j['name']}：定时开始已执行。")
                except Exception as e:
                    j['pause_at'] = None
                    self.save(j)
                    self.log(jid, f'定时开始失败：{e}')
                    notices.append(f"{j['name']}：{e}")
        return notices

    def tail(self, job_id, limit=150_000):
        p = self.directory(job_id) / 'render.log'
        if not p.exists():
            return '任务尚未启动。Blender 的实时输出和报错会显示在这里。'
        with p.open('rb') as f:
            f.seek(0, 2)
            length = f.tell()
            f.seek(max(0, length - limit))
            data = f.read()
        return ('…仅显示日志末尾，完整内容保存在日志文件中。\n' if length > limit else '') + data.decode('utf-8', errors='replace')
