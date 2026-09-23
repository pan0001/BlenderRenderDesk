"""Opt-in installation of verified, stable Windows releases from our fixed repository."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

import psutil
from .engine.protocol import read, write
from .version import VERSION

REPOSITORY = 'pan0001/BlenderRenderDesk'
RELEASES_URL = 'https://github.com/' + REPOSITORY + '/releases'
LATEST_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases/latest'
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024


def version_tuple(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', str(value))
    if not match:
        raise ValueError('发布版本号必须为 v主版本.次版本.修订号')
    return tuple(map(int, match.groups()))


def trusted_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('更新下载地址必须为官方 HTTPS 地址')
    if parts.hostname == 'api.github.com' and parts.path == '/repos/' + REPOSITORY + '/releases/latest':
        return url
    if parts.hostname == 'github.com' and parts.path.startswith('/' + REPOSITORY + '/releases/download/'):
        return url
    if parts.hostname in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
        return url
    raise ValueError('更新来源不是本项目的 GitHub Release')


class ReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        trusted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit, destination=None, progress=None, cancel=None):
    """Bounded streaming, including redirected URLs; never sends user credentials."""
    request = Request(trusted_url(url), headers={'User-Agent': 'BlenderRenderDesk/' + VERSION})
    opener = build_opener(ReleaseRedirect())
    chunks, count = [], 0
    output = destination.open('xb') if destination else None
    try:
        with opener.open(request, timeout=20) as response:
            length = int(response.headers.get('Content-Length') or 0)
            if length > limit:
                raise ValueError('更新文件超过大小限制')
            deadline = time.monotonic() + 900
            while True:
                if cancel and cancel.is_set():
                    raise ValueError('更新操作已停止')
                if time.monotonic() > deadline:
                    raise ValueError('下载超时，请重新下载')
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > limit:
                    raise ValueError('更新文件超过大小限制')
                if output:
                    output.write(chunk)
                else:
                    chunks.append(chunk)
                if progress:
                    progress(count)
            if length and count != length:
                raise ValueError('更新文件下载不完整')
        return count if output else b''.join(chunks)
    finally:
        if output:
            output.close()


def release_info(data, current=VERSION):
    tag = data.get('tag_name', '')
    if data.get('draft') or data.get('prerelease'):
        raise ValueError('仅支持正式发布版本')
    candidate = version_tuple(tag)
    if candidate <= version_tuple(current):
        return None
    canonical = 'v' + '.'.join(map(str, candidate))
    name = 'BlenderRenderDesk-' + canonical + '-Windows.zip'
    checksum_name = 'SHA256SUMS-' + canonical + '.txt'
    assets = {a['name']: a for a in data.get('assets', []) if a.get('state') == 'uploaded'}
    asset, checksum = assets.get(name), assets.get(checksum_name)
    if not asset or not checksum:
        raise ValueError('新版尚未上传完整的 Windows 包和 SHA256SUMS 校验文件')
    for a in (asset, checksum):
        url = a.get('browser_download_url', '')
        expected = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + a['name']
        if url != expected:
            raise ValueError('发布附件地址与版本不匹配')
    if not 0 < int(asset.get('size', 0)) <= MAX_ARCHIVE:
        raise ValueError('Windows 包大小无效')
    return {'version': canonical[1:], 'tag': tag, 'name': name, 'size': int(asset['size']),
            'url': asset['browser_download_url'], 'checksum_url': checksum['browser_download_url'],
            'digest': asset.get('digest'), 'notes': str(data.get('body') or '')[:24000],
            'page': RELEASES_URL + '/tag/' + tag}


def verify_archive(archive, info, checksums):
    wanted = re.findall(r'^([a-fA-F0-9]{64})\s+\*?' + re.escape(info['name']) + r'\s*$', checksums, re.M)
    if len(wanted) != 1:
        raise ValueError('校验文件中缺少唯一的 Windows 包 SHA256')
    hasher = hashlib.sha256()
    with archive.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    actual = hasher.hexdigest()
    if actual != wanted[0].lower() or archive.stat().st_size != info['size']:
        raise ValueError('更新包校验失败，请重新下载')
    if info.get('digest') and info['digest'].lower() != 'sha256:' + actual:
        raise ValueError('更新包与 GitHub 附件摘要不一致')
    return actual


def extract_archive(archive, directory, version):
    """No extractall: validate every Windows path and bound uncompressed content."""
    import zipfile
    names, total = set(), 0
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 20000:
            raise ValueError('更新包文件数量异常')
        for member in members:
            parts = PurePosixPath(member.filename).parts
            if not parts or parts[0] != 'BlenderRenderDesk' or '\\' in member.filename:
                raise ValueError('更新包目录结构无效')
            if any(p in ('.', '..') or p.endswith((' ', '.')) or any(c in p for c in ':<>"|?*') or p.split('.')[0].lower() in reserved for p in parts):
                raise ValueError('更新包包含不安全的路径')
            key = '/'.join(parts).casefold()
            mode = member.external_attr >> 16
            if key in names or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)) or member.flag_bits & 1:
                raise ValueError('更新包包含重复、链接或加密文件')
            names.add(key)
            total += member.file_size
            if total > MAX_EXPANDED:
                raise ValueError('更新包解压体积超出限制')
        if shutil.disk_usage(directory).free < total + 64 * 1024 * 1024:
            raise ValueError('磁盘空间不足，无法准备更新')
        for member in members:
            target = directory.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open('xb') as dest:
                    shutil.copyfileobj(source, dest, length=1024 * 1024)
    app = directory / 'BlenderRenderDesk'
    if not (app / 'BlenderRenderDesk.exe').is_file() or not (app / '_internal').is_dir():
        raise ValueError('更新包缺少 Windows 程序或运行库')
    if (app / 'VERSION').read_text(encoding='utf-8').strip() != version:
        raise ValueError('更新包内部版本不匹配')
    return app


def check_render_locations(install, snapshot):
    """Do not move a user's project, renderer, script or output with the app backup."""
    install = Path(install).resolve()
    for task in snapshot.get('tasks', []):
        job = task['job']
        paths = [job.get(key) for key in ('blend', 'blender', 'output')]
        for key in ('external', 'batch'):
            group = job.get(key) or {}
            paths.extend(group.get(field) for field in ('script', 'cwd', 'python'))
        for value in paths:
            if isinstance(value, str) and value:
                path = Path(value).resolve()
                if path == install or install in path.parents:
                    raise ValueError('任务工程、脚本或输出位于软件目录内，请将软件单独放置后再使用内置更新')


class Updater:
    def __init__(self, root, shutdown=None, preflight=None):
        self.root = Path(root).resolve()
        self.directory = self.root / 'updates'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.install = Path(sys.executable).resolve().parent
        self.shutdown = shutdown
        self.preflight = preflight
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.info = None
        self.staged = None
        self.stage_root = None
        self.archive = None
        self.digest = None
        self.supported = os.name == 'nt' and getattr(sys, 'frozen', False) and Path(sys.executable).name.lower() == 'blenderrenderdesk.exe'
        if self.root == self.install or self.install in self.root.parents:
            self.supported = False
        config = read(self.directory / 'settings.json', {})
        self.state = {'current': VERSION, 'phase': 'idle', 'message': '尚未检查更新', 'latest': None,
                      'auto_check': bool(config.get('auto_check', True)), 'supported': bool(self.supported),
                      'received': 0, 'total': 0, 'result': read(self.directory / 'result.json', {})}

    def snapshot(self):
        with self.lock:
            try:
                self.state['result'] = read(self.directory / 'result.json', {})
            except (OSError, ValueError):
                pass
            return {**self.state}

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def configure(self, auto_check):
        if not isinstance(auto_check, bool):
            raise ValueError('自动检查设置必须是布尔值')
        with self.lock:
            write(self.directory / 'settings.json', {'auto_check': auto_check})
            self.state['auto_check'] = auto_check
        return self.snapshot()

    def _run(self, action, phase):
        with self.lock:
            if self.state['phase'] == 'installing' or (self.thread and self.thread.is_alive()):
                raise ValueError('已有更新操作进行中')
            self._set(phase=phase, message={'checking': '正在查询 GitHub Release…', 'downloading': '正在下载 Windows 更新包…'}[phase])
            def work():
                try:
                    action()
                except HTTPError as error:
                    message = 'GitHub 暂时限制请求，请稍后再试' if error.code in (403, 429) else '尚无可用的正式 Release' if error.code == 404 else 'GitHub 请求失败，请稍后重试'
                    self._set(phase='error', message=message)
                except Exception as error:
                    message = str(error) if isinstance(error, ValueError) else '更新失败，请检查网络、磁盘空间及程序目录写入权限后重试'
                    self._set(phase='error', message=message)
            self.thread = threading.Thread(target=work, daemon=True, name='Release updater')
            self.thread.start()
        return self.snapshot()

    def check(self):
        def work():
            info = release_info(json.loads(fetch(LATEST_URL, 2 * 1024 * 1024)))
            self.info = info
            self._set(latest=info, phase='available' if info else 'current', received=0, total=info['size'] if info else 0,
                      message=('发现新版本 v' + info['version']) if info else '当前已是最新版本（或高于 GitHub 已发布版本）')
        return self._run(work, 'checking')

    def download(self):
        if not self.supported:
            raise ValueError('仅支持 Windows 成品包内更新；源码运行或数据位于程序目录内时请手动更新')
        if not self.info:
            raise ValueError('请先检查是否有新版本')
        def work():
            self._cleanup_stage()
            info = self.info.copy()
            # A sibling staging directory guarantees same-volume, fast directory moves.
            self.stage_root = Path(tempfile.mkdtemp(prefix='.renderdesk-update-', dir=self.install.parent))
            self.archive = self.stage_root / 'update.zip'
            self._set(received=0, total=info['size'])
            fetch(info['url'], info['size'], self.archive, lambda n: self._set(received=n), self.cancel)
            self._set(phase='verifying', message='下载完成，正在校验并准备安装…')
            checksums = fetch(info['checksum_url'], 64 * 1024).decode('utf-8-sig')
            self.digest = verify_archive(self.archive, info, checksums)
            self.staged = extract_archive(self.archive, self.stage_root, info['version'])
            self._set(phase='ready', message='校验通过，可以安装并重启管理器')
        return self._run(work, 'downloading')

    def install_update(self):
        with self.lock:
            if self.state['phase'] != 'ready' or (self.thread and self.thread.is_alive()):
                raise ValueError('更新包尚未准备好')
            if not self.supported or not self.shutdown:
                raise ValueError('当前运行方式不支持安装更新')
            if self.install.parent == self.install or (self.install / '.git').exists():
                raise ValueError('请使用独立解压的 Windows 成品包')
            if self.preflight:
                self.preflight(self.install)
            # Data, staging and backups are separate; the helper never touches task files.
            backup = self.install.with_name(self.install.name + '-backup-' + self.stage_root.name[-8:])
            ready = self.directory / (self.stage_root.name + '-ready.json')
            helper = self.directory / (self.stage_root.name + '.ps1')
            shutil.copyfile(Path(__file__).parent / 'engine' / 'install_update.ps1', helper)
            arguments = list(sys.argv[1:])
            for option in ('--update-ready', '--data-dir'):
                while option in arguments:
                    index = arguments.index(option)
                    del arguments[index:index + 2]
                arguments = [a for a in arguments if not a.startswith(option + '=')]
            arguments = [a for a in arguments if a != '--smoke-test']
            arguments += ['--data-dir', str(self.root)]
            plan = {'pid': os.getpid(), 'birth': psutil.Process().create_time(), 'install': str(self.install),
                    'staged': str(self.staged), 'backup': str(backup), 'stage_root': str(self.stage_root),
                    'ready': str(ready), 'version': self.info['version'], 'result': str(self.directory / 'result.json'),
                    'arguments': subprocess.list2cmdline(arguments),
                    'updated_arguments': subprocess.list2cmdline(arguments + ['--update-ready', str(ready)])}
            plan_file = helper.with_suffix('.json')
            write(plan_file, plan)
            powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-Plan', str(plan_file)],
                             cwd=self.directory, creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
            self._set(phase='installing', message='正在重启管理器以安装更新，Blender 渲染继续运行…')
            # Allow the HTTP response to reach local and remote UIs before closing the server.
            timer = threading.Timer(1.0, self.shutdown)
            timer.daemon = True
            timer.start()
            return self.snapshot()

    def _cleanup_stage(self):
        if self.stage_root and self.stage_root.parent == self.install.parent and self.stage_root.name.startswith('.renderdesk-update-'):
            shutil.rmtree(self.stage_root)
        self.stage_root = self.staged = self.archive = self.digest = None

    def close(self):
        self.cancel.set()
        # Installing owns its staging directory; do not remove it during handoff.
        if self.state['phase'] != 'installing' and (not self.thread or not self.thread.is_alive()):
            try:
                self._cleanup_stage()
            except OSError:
                pass
