"""Original scheduler and process controller. No GUI dependency, no license gates."""
import codecs
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
import psutil
from .processes import Sampler, process
from .engine.protocol import frame_file, frames, png_complete, read, receipt, signature, verify, write
from .storage import Catalogue

LABELS = {'ready': '等待开始', 'starting': '启动中', 'loading': '载入工程', 'rendering': '正在渲染',
          'pausing': '保存当前帧后退出', 'watching': '等待新帧保存后停止', 'paused': '已暂停 · 资源已释放',
          'finishing': '正在退出进程', 'complete': '全部完成', 'interrupted': '已中断 · 可继续', 'error': '发生错误'}
ACTIVE = {'starting', 'loading', 'rendering', 'pausing', 'watching'}


def default_root():
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'BlenderRenderDesk'


class LogReader:
    def __init__(self):
        self.path, self.position = None, 0
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def poll(self, path):
        path = Path(path)
        size = path.stat().st_size if path.exists() else 0
        reset = path != self.path or size < self.position
        if reset:
            self.path, self.position = path, max(0, size - 65536)
            self.decoder.reset()
        if size == self.position:
            return reset, ''
        with path.open('rb') as stream:
            stream.seek(self.position)
            block = stream.read(32768)
            self.position = stream.tell()
        return reset, self.decoder.decode(block)


class Controller:
    def __init__(self, root=None):
        self.root = Path(root or default_root()).resolve()
        self.catalogue = Catalogue(self.root)
        self.jobs = self.catalogue.jobs()
        self.sampler = Sampler()
        self.children, self.frame_cache = {}, {}
        self.notices = list(self.catalogue.issues)
        self.log_reader = LogReader()

    def directory(self, jid):
        if jid not in self.jobs:
            raise ValueError('找不到任务')
        return self.root / 'jobs' / jid

    def save(self, job):
        self.catalogue.save(job)
        self.jobs[job['id']] = job

    def event(self, jid, message):
        with (self.directory(jid) / 'render.log').open('a', encoding='utf-8') as stream:
            stream.write(f'\n[RenderDesk {time.strftime("%H:%M:%S")}] {message}\n')

    def live(self, jid):
        child = self.children.get(jid)
        if child and child.poll() is not None:
            del self.children[jid]
        return process(read(self.directory(jid) / 'launch.json', {})) is not None

    def validate(self, values):
        values = dict(values)
        for key in ('start', 'end', 'step', 'threads'):
            values[key] = int(values.get(key, 0 if key == 'threads' else 1))
        if not -1048574 <= values['start'] <= values['end'] <= 1048574 or values['step'] < 1:
            raise ValueError('请检查起止帧和步长')
        if len(frames(values)) > 100000:
            raise ValueError('单任务最多支持 100000 帧')
        if not 0 <= values['threads'] <= 1024:
            raise ValueError('线程数需在 0–1024 之间')
        for key in ('blend', 'blender'):
            values[key] = str(Path(values[key]).resolve())
            if not Path(values[key]).is_file():
                raise ValueError(f'文件不存在：{values[key]}')
        if Path(values['blend']).suffix.lower() != '.blend':
            raise ValueError('请选择 .blend 工程')
        if values.get('format', 'PNG') not in ('PNG', 'OPEN_EXR', 'OPEN_EXR_MULTILAYER', 'JPEG', 'TIFF', 'BMP', 'TARGA', 'TARGA_RAW', 'IRIS', 'JPEG2000', 'HDR', 'WEBP'):
            raise ValueError('该输出格式暂不支持按帧保存')
        values.setdefault('format', 'PNG')
        values.setdefault('autoexec', False)
        return values

    def add(self, values):
        job = self.validate(values)
        jid = uuid.uuid4().hex[:12]
        job.update(id=jid, name=Path(job['blend']).stem, created_at=time.time(),
                   source=signature(job['blend']), start_at=None, pause_at=None, elapsed_base=0)
        output = Path(job['output']).resolve() / f'{job["name"]}_{jid}'
        output.mkdir(parents=True, exist_ok=False)
        job['output'] = str(output)
        self.save(job)
        write(self.directory(jid) / 'job.json', job)
        self.event(jid, '任务已创建；图像将保存到独立任务目录。')
        return jid

    def add_project(self, values):
        job = self.validate(values)
        if signature(job['blend']) != values['source']:
            raise ValueError('扫描后工程发生变化，请重新扫描')
        jid = uuid.uuid4().hex[:12]
        job.update(id=jid, name=Path(job['blend']).stem, created_at=time.time(),
                   start_at=None, pause_at=None, elapsed_base=0)
        self.save(job)
        write(self.directory(jid) / 'job.json', job)
        self.event(jid, '已从工程读取渲染配置，保留原输出路径和图像格式；已有未确认文件不会被覆盖。')
        return jid

    def rescan(self, jid, values):
        job = self.jobs[jid]
        attached = job.get('external', {}).get('phase') == 'attached'
        if self.live(jid) and not attached:
            raise ValueError('请先按帧暂停任务，再应用新的工程范围')
        if values['source'] != job['source']:
            raise ValueError('工程内容已修改，请新建任务，避免混用之前的完成帧')
        updated = dict(job)
        updated.update(project=values['project'], start=values['start'], end=values['end'],
                       step=values['step'], range_source='project')
        if job.get('preserve_project') and not job.get('external'):
            updated['project_paths'] = values['project_paths']
        records = read(self.directory(jid) / 'progress.json', {'done': {}})['done']
        for frame, record in records.items():
            if int(frame) not in frames(updated) or Path(record['path']).resolve() != frame_file(updated, int(frame)).resolve():
                raise ValueError('工程范围或路径与已完成帧不匹配，请新建任务')
        self.save(updated)
        write(self.directory(jid) / 'job.json', updated)
        state = read(self.directory(jid) / 'status.json', {})
        if state.get('state') == 'complete' and len(records) < len(frames(updated)):
            write(self.directory(jid) / 'status.json', {**state, 'state': 'interrupted'})
        self.event(jid, f"已重新读取工程范围：{updated['start']}–{updated['end']}，共 {len(frames(updated))} 帧")
        return jid

    def status(self, jid, row=None):
        directory = self.directory(jid)
        state = read(directory / 'status.json', {'state': 'ready'})
        progress = read(directory / 'progress.json', {'done': {}})['done']
        launch = read(directory / 'launch.json', {})
        live = self.live(jid)
        label = state.get('state', 'ready')
        if live:
            if label in ('paused', 'complete', 'error'):
                label = 'finishing'
            elif read(directory / 'control.json', {}).get('pause'):
                label = 'watching' if self.jobs[jid].get('external', {}).get('phase') == 'attached' else 'pausing'
        elif label in ACTIVE:
            label = 'paused' if self.jobs[jid].get('external', {}).get('phase') == 'attached' and read(directory / 'external_stop.json') else 'interrupted'
        elapsed = state.get('elapsed_total', self.jobs[jid].get('elapsed_base', 0))
        if live:
            elapsed = self.jobs[jid].get('elapsed_base', 0) + max(0, time.time() - launch.get('created', time.time()))
        elif not elapsed and launch.get('created'):
            elapsed = max(0, state.get('updated', launch['created']) - launch['created'])
        seconds = [r['seconds'] for r in progress.values() if r.get('seconds', 0) > 0]
        total = len(frames(self.jobs[jid]))
        return {**state, **(row or {}), 'state': label, 'running': live, 'pid': launch.get('pid') if live else None,
                'done': len(progress), 'total': total, 'elapsed': elapsed,
                'latest': self.latest(jid, progress),
                'eta': (sum(seconds) / len(seconds) * (total - len(progress))) if seconds and live else None}

    def latest(self, jid, records):
        if not records:
            return None
        f, r = max(records.items(), key=lambda pair: pair[1].get('mtime_ns', 0))
        return {'frame': int(f), 'version': str(r.get('mtime_ns', 0)) + '-' + str(r.get('size', 0))}

    def start(self, jid):
        job, directory = self.jobs[jid], self.directory(jid)
        if self.live(jid):
            raise ValueError('任务已经在运行')
        if signature(job['blend']) != job['source']:
            raise ValueError('工程已修改，请新建任务，避免混用不同版本的帧')
        for other in self.jobs.values():
            if other['id'] != jid and self.live(other['id']) and Path(other['output']).resolve() == Path(job['output']).resolve():
                raise ValueError('该输出目录已有运行中的任务，请等待或更换输出目录')
        ext = job.get('external')
        if ext and signature(ext['script']) != ext['script_source']:
            raise ValueError('原渲染脚本已修改，不能安全续渲染')
        valid = {}
        for frame, record in read(directory / 'progress.json', {'done': {}})['done'].items():
            if int(frame) not in frames(job) or Path(record['path']).resolve() != frame_file(job, int(frame)).resolve():
                raise ValueError('完成帧记录与任务不匹配')
            if verify(record):
                valid[frame] = record
        if ext:
            valid = self.inspect_external(job)
            for frame in frames(job):
                path = frame_file(job, frame)
                if path.exists() and str(frame) not in valid:
                    path.rename(path.with_name(path.name + '.incomplete-' + uuid.uuid4().hex[:8]))
        if len(valid) == len(frames(job)):
            raise ValueError('全部帧已完成')
        job['elapsed_base'] = self.status(jid)['elapsed']
        job['start_at'] = None
        if ext:
            ext['phase'] = 'managed'
        self.save(job)
        write(directory / 'job.json', job)
        write(directory / 'progress.json', {'done': valid})
        write(directory / 'control.json', {'pause': False, 'pause_at': job.get('pause_at')})
        write(directory / 'status.json', {'state': 'starting', 'updated': time.time(), 'elapsed_total': job['elapsed_base']})
        source = Path(__file__).parent / 'engine'
        for filename in ('runner.py', 'protocol.py'):
            shutil.copyfile(source / filename, directory / filename)
        if ext:
            command = list(ext['args'])
            command[command.index('--python') + 1] = str(directory / 'runner.py')
        else:
            command = [job['blender'], '-b', '--enable-autoexec' if job.get('autoexec') else '--disable-autoexec', job['blend'], '--python-exit-code', '23']
            if job.get('threads'):
                command += ['-t', str(job['threads'])]
            command += ['--python', str(directory / 'runner.py'), '--', str(directory)]
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1', 'RENDERDESK_JOB_DIR': str(directory)}
        self.event(jid, '启动 / 继续任务：' + subprocess.list2cmdline(command))
        try:
            with (directory / 'render.log').open('ab', buffering=0) as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         cwd=ext['cwd'] if ext else str(Path(job['blend']).parent), env=env,
                                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.children[jid] = child
            write(directory / 'launch.json', {'pid': child.pid, 'created': psutil.Process(child.pid).create_time()})
        except Exception as error:
            write(directory / 'status.json', {'state': 'error', 'message': str(error), 'updated': time.time()})
            raise

    def pause(self, jid):
        if not self.live(jid):
            raise ValueError('此任务没有运行中的 Blender')
        job = self.jobs[jid]
        control = {'pause': True}
        if job.get('external', {}).get('phase') == 'attached':
            control['baseline'] = self.inspect_external(job)
        write(self.directory(jid) / 'control.json', control)
        job.update(start_at=None, pause_at=None)
        self.save(job)
        self.event(jid, '请求暂停；等待当前帧保存并退出 Blender。')

    def schedule(self, jid, start_at=None, pause_at=None):
        now = time.time()
        if any(value is not None and value <= now for value in (start_at, pause_at)):
            raise ValueError('计划时间必须在未来')
        if start_at and pause_at and pause_at <= start_at:
            raise ValueError('暂停时间必须晚于开始时间')
        job = self.jobs[jid]
        job.update(start_at=start_at, pause_at=pause_at)
        self.save(job)
        control = read(self.directory(jid) / 'control.json', {})
        control['pause_at'] = pause_at
        write(self.directory(jid) / 'control.json', control)
        self.event(jid, '定时计划已保存' if start_at or pause_at else '定时计划已清除')

    def attach(self, values):
        row = values['process']
        item = process(row)
        if not item:
            raise ValueError('进程已退出，请刷新列表')
        args = item.cmdline()
        if not any(a in args for a in ('-b', '--background')) or args.count('--python') != 1 or any(a in args for a in ('--python-expr', '-a', '--render-anim', '-f', '--render-frame')):
            raise ValueError('接入支持单个 --python 脚本逐帧 PNG 后台渲染；此进程目前仅可监测')
        cwd = item.cwd()
        script = Path(args[args.index('--python') + 1])
        script = (Path(cwd) / script).resolve() if not script.is_absolute() else script
        blend = next((a for a in args if a.lower().endswith('.blend')), '')
        if not blend:
            raise ValueError('启动参数没有 .blend 路径')
        blend = str((Path(cwd) / blend).resolve())
        job = self.validate({**values, 'blend': blend, 'blender': item.exe(), 'format': 'PNG'})
        job.pop('process', None)
        job.pop('project_paths', None)
        pattern = values['pattern']
        if not re.fullmatch(r'[^/\\#{}]*#{1,10}[^/\\#{}]*\.png', pattern, re.I):
            raise ValueError('文件名模板应类似 ####.png 或 frame_######.png')
        if not script.is_file() or not Path(job['output']).is_dir():
            raise ValueError('脚本或输出文件夹不存在')
        for existing in self.jobs:
            launch = read(self.directory(existing) / 'launch.json', {})
            if self.live(existing) and (launch.get('pid') == item.pid or Path(self.jobs[existing]['output']).resolve() == Path(job['output']).resolve()):
                raise ValueError('此进程或输出目录已经被管理')
        jid = uuid.uuid4().hex[:12]
        job.update(id=jid, name=Path(blend).stem, output=str(Path(job['output']).resolve()), created_at=time.time(),
                   source=signature(blend), start_at=None, pause_at=None,
                   external={'phase': 'attached', 'script': str(script), 'script_source': signature(script),
                             'args': args, 'cwd': cwd, 'pattern': pattern})
        self.save(job)
        directory = self.directory(jid)
        write(directory / 'job.json', job)
        write(directory / 'launch.json', {'pid': item.pid, 'created': item.create_time()})
        write(directory / 'status.json', {'state': 'rendering', 'updated': time.time()})
        write(directory / 'progress.json', {'done': self.inspect_external(job)})
        self.event(jid, '外部进程已接入。首次暂停检测下一张完整 PNG 后结束原进程，可能丢弃随后开始的帧。旧 stdout 无法补接；续渲染后记录完整日志。')
        return jid

    def inspect_external(self, job):
        cache = self.frame_cache.setdefault(job['id'], {})
        done = {}
        pattern = job['external']['pattern']
        mark = re.search(r'#+', pattern)
        matcher = re.compile(re.escape(pattern[:mark.start()]) + r'(-?\d+)' + re.escape(pattern[mark.end():]))
        with os.scandir(job['output']) as entries:
            for entry in entries:
                match = matcher.fullmatch(entry.name)
                if not match or int(match[1]) not in frames(job):
                    continue
                frame = int(match[1])
                path = frame_file(job, frame)
                if path.name != entry.name:
                    continue
                try:
                    before = signature(path)
                    cached = cache.get(str(path))
                    if not cached or cached[0] != before:
                        data = {**before, 'path': str(path)} if png_complete(path) and before == signature(path) else None
                        cache[str(path)] = before, data
                    if cache[str(path)][1]:
                        done[str(frame)] = cache[str(path)][1]
                except OSError:
                    continue
        return done

    def observe_external(self, job):
        directory = self.directory(job['id'])
        done = self.inspect_external(job)
        previous = read(directory / 'progress.json', {'done': {}})['done']
        if done != previous:
            write(directory / 'progress.json', {'done': done})
            for frame in done.keys() - previous.keys():
                self.event(job['id'], f'检测到完整 PNG：{frame}')
        identity = read(directory / 'launch.json', {})
        live = process(identity)
        control = read(directory / 'control.json', {})
        stopped = read(directory / 'external_stop.json', {})
        if live and control.get('pause') and not stopped and any(control.get('baseline', {}).get(f) != r for f, r in done.items()):
            # Recheck PID + birth time immediately before terminating the selected process.
            selected = process(identity)
            if selected:
                selected.terminate()
                write(directory / 'external_stop.json', {'requested': time.time()})
                self.event(job['id'], '新帧保存完成，已请求结束原 Blender 进程')
        if not process(identity):
            state = 'complete' if len(done) == len(frames(job)) else ('paused' if read(directory / 'external_stop.json') else 'interrupted')
            write(directory / 'status.json', {'state': state, 'updated': time.time(), 'elapsed_total': max(0, time.time() - identity.get('created', time.time()))})
            job['external']['phase'] = 'managed'
            self.save(job)

    def tick(self, now=None):
        now = time.time() if now is None else now
        for jid, job in list(self.jobs.items()):
            try:
                if job.get('external', {}).get('phase') == 'attached':
                    self.observe_external(job)
                if job.get('pause_at') and now >= job['pause_at']:
                    job.update(start_at=None, pause_at=None)
                    self.save(job)
                    if self.live(jid):
                        self.pause(jid)
                    else:
                        self.event(jid, '计划窗口已结束，取消过期启动')
                elif job.get('start_at') and now >= job['start_at']:
                    job['start_at'] = None
                    self.save(job)
                    if not self.live(jid):
                        self.start(jid)
            except Exception as error:
                message = f'{job["name"]}：{error}'
                if not self.notices or self.notices[-1] != message:
                    self.notices.append(message)
                    self.notices = self.notices[-20:]

    def snapshot(self):
        self.tick()
        processes, system = self.sampler.scan()
        indexed = {(r['pid'], r['created']): r for r in processes}
        tasks = []
        for jid, job in self.jobs.items():
            try:
                identity = read(self.directory(jid) / 'launch.json', {})
                metrics = indexed.get((identity.get('pid'), identity.get('created')))
                tasks.append({'job': {k: v for k, v in job.items() if k != 'project_paths'}, 'status': self.status(jid, metrics)})
            except Exception as error:
                tasks.append({'job': {k: v for k, v in job.items() if k != 'project_paths'}, 'status': {'state': 'error', 'message': str(error), 'done': 0, 'total': len(frames(job)), 'elapsed': 0}})
        return {'tasks': tasks, 'processes': processes, 'system': system, 'notices': self.notices[-5:]}

    def close(self):
        self.sampler.close()
        self.catalogue.close()
