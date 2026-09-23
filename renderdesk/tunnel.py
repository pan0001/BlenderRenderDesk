"""Manage only our own frpc child and one Render Desk TCP proxy."""
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit
from .engine.protocol import read, write


class Frpc:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'frpc-settings.json'
        self.config = read(self.path, {'server': '', 'port': 7000, 'remote_port': 18765, 'name': 'renderdesk', 'token': '', 'autostart': False})
        self.binary = Path(__file__).resolve().parent.parent / 'vendor/frpc/frpc.exe'
        self.child = None
        self.lock = threading.RLock()
        self.message = ''
        self.log_path = self.root / 'frpc.log'

    def settings(self):
        # Configuration is replaced atomically. Snapshot reads must not wait for frpc verify/start.
        config, child = self.config, self.child
        return {k: v for k, v in config.items() if k != 'token'} | {
            'configured': bool(config.get('token')), 'available': self.binary.is_file(),
            'running': child is not None and child.poll() is None, 'message': self.message,
            'version': '0.71.0', 'log': self.log()}

    def log(self):
        if not self.log_path.exists():
            return ''
        with self.log_path.open('rb') as stream:
            stream.seek(max(0, self.log_path.stat().st_size - 8192))
            value = stream.read(8192).decode('utf8', 'replace')
        if self.config.get('token'):
            value = value.replace(self.config['token'], '[redacted]')
        return value

    def configure(self, values):
        with self.lock:
            if self.child and self.child.poll() is None:
                raise ValueError('请先停止 frpc 再修改连接设置')
            server = str(values.get('server', '')).strip()
            if not server or len(server) > 253 or not re.fullmatch(r'[\w.:-]+', server, re.ASCII):
                raise ValueError('请填写 frps 主机名或 IP，不要包含协议和路径')
            port, remote_port = int(values.get('port', 7000)), int(values.get('remote_port', 18765))
            if not 1 <= port <= 65535 or not 1 <= remote_port <= 65535:
                raise ValueError('frp 端口需在 1–65535 之间')
            name = str(values.get('name', 'renderdesk'))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', name):
                raise ValueError('代理名称仅支持 1–48 个英文字母、数字、下划线或短横线')
            token = str(values.get('token', '')).strip() or self.config.get('token', '')
            self.config = {'server': server, 'port': port, 'remote_port': remote_port, 'name': name,
                           'token': token, 'autostart': bool(values.get('autostart'))}
            write(self.path, self.config)
            return self.settings()

    def start(self):
        with self.lock:
            if self.child and self.child.poll() is None:
                raise ValueError('frpc 已在运行')
            if not self.binary.is_file() or not self.config.get('server'):
                raise ValueError('frpc 程序或 frps 地址未配置')
            status = read(self.root / 'server-status.json', {})
            local_port = urlsplit(status.get('url', '')).port
            if not local_port:
                raise ValueError('本地网页服务尚未启动')
            cfg = self.config
            proxy = {'serverAddr': cfg['server'], 'serverPort': cfg['port'], 'loginFailExit': True,
                     'auth': {'method': 'token', 'token': cfg.get('token', '')},
                     'transport': {'tls': {'enable': True}},
                     'proxies': [{'name': cfg['name'], 'type': 'tcp', 'localIP': '127.0.0.1',
                                  'localPort': local_port, 'remotePort': cfg['remote_port']}]}
            destination = self.root / 'frpc-managed.json'
            write(destination, proxy)
            result = subprocess.run([str(self.binary), 'verify', '-c', str(destination)], capture_output=True,
                                    timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise ValueError('frpc 配置校验失败，请检查地址和端口')
            # Each explicit start begins a fresh bounded log. No secrets are sent to the UI.
            with self.log_path.open('wb') as log:
                self.child = subprocess.Popen([str(self.binary), '-c', str(destination)], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.message = 'frpc 已启动，请在日志确认代理连接成功'
            return self.settings()

    def stop(self):
        with self.lock:
            if self.child and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=5)
            self.child = None
            self.message = 'frpc 已停止'
            return self.settings()

    def close(self):
        self.stop()
