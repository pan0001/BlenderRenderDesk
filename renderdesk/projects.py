"""Global Blender installations and background, read-only .blend inspection."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from .engine.protocol import read, write, signature
from .processes import installations


class BlenderRegistry:
    def __init__(self, root):
        self.path = Path(root) / 'blenders.json'
        self.lock = threading.RLock()
        self.data = read(self.path, {'items': [], 'default': ''})
        for exe in (installations() if not self.path.exists() else []):
            if not any(i['path'].lower() == exe.lower() for i in self.data['items']):
                self._append(exe, Path(exe).parent.name)
        if not self.data['default'] and self.data['items']:
            self.data['default'] = self.data['items'][0]['id']
        write(self.path, self.data)

    def _append(self, path, name):
        ident = hashlib.sha256(str(Path(path).resolve()).lower().encode()).hexdigest()[:16]
        self.data['items'].append({'id': ident, 'path': str(Path(path).resolve()), 'name': name})
        return ident

    def snapshot(self):
        import copy
        with self.lock:
            return copy.deepcopy(self.data)

    def resolve(self, ident=None):
        with self.lock:
            ident = ident or self.data['default']
            item = next((i for i in self.data['items'] if i['id'] == ident), None)
            if not item or not Path(item['path']).is_file():
                raise ValueError('请先在全局设置中添加有效 Blender 版本并设为默认')
            return item['path']

    def update(self, values):
        path, version = None, []
        if values.get('path'):
            path = Path(values['path']).resolve()
            if not path.is_file():
                raise ValueError('Blender 程序不存在')
            result = subprocess.run([str(path), '--version'], capture_output=True, timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            version = result.stdout.decode('utf8', 'replace').splitlines()
            if result.returncode or not version or not version[0].startswith('Blender '):
                raise ValueError('该程序没有返回有效的 Blender 版本')
        with self.lock:
            if values.get('remove'):
                self.data['items'] = [i for i in self.data['items'] if i['id'] != values['remove']]
                if self.data['default'] == values['remove']:
                    self.data['default'] = self.data['items'][0]['id'] if self.data['items'] else ''
            if path:
                existing = next((i for i in self.data['items'] if Path(i['path']) == path), None)
                ident = existing['id'] if existing else self._append(str(path), values.get('name') or version[0])
                if not self.data['default'] or values.get('make_default'):
                    self.data['default'] = ident
            if values.get('default'):
                if not any(i['id'] == values['default'] for i in self.data['items']):
                    raise ValueError('找不到该 Blender 版本')
                self.data['default'] = values['default']
            write(self.path, self.data)
            return self.snapshot()


def scan_project(blender, blend):
    blend = Path(blend).resolve()
    if not blend.is_file() or blend.suffix.lower() != '.blend':
        raise ValueError('请选择渲染电脑上的 .blend 工程')
    before = signature(blend)
    with tempfile.TemporaryDirectory(prefix='renderdesk-probe-') as directory:
        output = Path(directory) / 'scan.json'
        result = subprocess.run([str(blender), '--background', '--disable-autoexec', str(blend),
            '--python-exit-code', '23', '--python', str(Path(__file__).parent/'engine/probe.py'), '--', str(output)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or not output.exists():
            raise ValueError('工程扫描失败：' + result.stdout.decode('utf8', 'replace')[-1200:])
        if signature(blend) != before:
            raise ValueError('扫描期间工程文件发生变化，请重试')
        return {**read(output), 'blend': str(blend), 'blender': str(blender), 'source': before,
                'scanned_at': time.time()}


def scene_values(scan, scene_name=None, overrides=None):
    """Construct a task from Blender's own paths; only explicit overrides change it."""
    from .engine.protocol import frames
    scene = next((s for s in scan['scenes'] if s['scene'] == (scene_name or scan['active_scene'])), None)
    if not scene:
        raise ValueError('找不到选定场景')
    if scene['format'] in ('FFMPEG', 'AVI_RAW', 'AVI_JPEG'):
        raise ValueError('工程使用视频输出。按帧暂停需要图像序列，请在 Blender 中设置后重新扫描；不会自动更改工程格式。')
    if scene.get('use_multiview'):
        raise ValueError('该场景启用了多视图，当前帧保存协议暂不支持')
    meta = {k: v for k, v in scene.items() if k != 'project_paths'}
    values = {**scene, 'blend': scan['blend'], 'blender': scan['blender'], 'project': meta,
              'source': scan['source'], 'preserve_project': True, 'autoexec': False,
              'range_source': 'project', 'threads': 0}
    overrides = overrides or {}
    if overrides.get('range'):
        start, end, step = (int(overrides['range'][key]) for key in ('start', 'end', 'step'))
        if start < scene['start'] or end > scene['end'] or start > end or step < 1:
            raise ValueError('覆盖范围必须位于工程范围内')
        chosen = range(start, end + 1, step)
        if any(str(f) not in scene['project_paths'] for f in chosen):
            raise ValueError('覆盖范围必须遵循工程的帧步长')
        values.update(start=start, end=end, step=step, range_source='override')
    paths = {str(f): scene['project_paths'][str(f)] for f in frames(values)}
    if overrides.get('output'):
        folder = Path(overrides['output']).resolve()
        paths = {f: str(folder / Path(p).name) for f, p in paths.items()}
        values['output_override'] = True
    values['project_paths'] = paths
    values['output'] = str(Path(next(iter(paths.values()))).parent)
    values['autoexec'] = bool(overrides.get('autoexec'))
    return values


def process_project(row):
    from .processes import process
    item = process(row)
    if not item:
        raise ValueError('进程已退出，请刷新列表')
    args, cwd = item.cmdline(), item.cwd()
    blend = next((a for a in args if a.lower().endswith('.blend')), '')
    if not blend:
        raise ValueError('进程启动参数中没有工程路径')
    return item.exe(), str((Path(cwd) / blend).resolve()), args


def public_scan(scan):
    return {**scan, 'scenes': [{k: v for k, v in s.items() if k != 'project_paths'} for s in scan['scenes']]}
