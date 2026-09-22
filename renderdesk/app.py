"""pywebview desktop shell, or the identical UI as a standalone web service."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from .engine.protocol import read, write
from .service import default_root
from .runtime import Runtime
from .server import create_app


def workspace_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / 'manager.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('此数据目录已有管理器运行，请先关闭旧管理器。')
    return handle


class DesktopBridge:
    def __init__(self):
        self._window = None

    def pick(self, kind):
        import webview
        if kind not in ('blend', 'blender', 'output'):
            return None
        if kind == 'output':
            result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        else:
            filters = ('Blender project (*.blend)',) if kind == 'blend' else ('Programs (*.exe)', 'All files (*.*)')
            result = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False, file_types=filters)
        return result[0] if result else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=default_root())
    parser.add_argument('--server', action='store_true', help='Run without a desktop window')
    parser.add_argument('--remote', action='store_true', help='Listen on all network interfaces with bearer authentication')
    parser.add_argument('--port', type=int)
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    lock = workspace_lock(args.data_dir)
    runtime = None
    server = None
    try:
        config = read(args.data_dir / 'server.json', {})
        remote = args.remote or config.get('remote', False)
        port = args.port if args.port is not None else (config.get('port', 8765) if remote or args.server else 0)
        if not 0 <= port <= 65535:
            raise ValueError('端口无效')
        token_path = args.data_dir / 'access-token.json'
        token = read(token_path, {}).get('token')
        if not token:
            token = secrets.token_urlsafe(32)
            write(token_path, {'token': token})
        runtime = Runtime(args.data_dir)
        from waitress import create_server
        server = create_server(create_app(runtime, token, remote), host='0.0.0.0' if remote else '127.0.0.1', port=port, threads=6)
        actual_port = server.effective_port
        url = f'http://127.0.0.1:{actual_port}/'
        write(args.data_dir / 'server-status.json', {'url': url, 'remote': remote, 'pid': os.getpid()})
        threading.Thread(target=server.run, daemon=True, name='HTTP server').start()
        if args.server:
            print(f'Render Desk: {url} | access key: {token_path}', flush=True)
            if args.smoke_test:
                return 0
            try:
                while True:
                    time.sleep(.5)
            except KeyboardInterrupt:
                return 0
        import webview
        bridge = DesktopBridge()
        window = webview.create_window('Blender Render Desk 3', url + '#token=' + token,
                                       js_api=bridge, width=1380, height=900, min_size=(780, 580), background_color='#10151c')
        bridge._window = window
        if args.smoke_test:
            def smoke():
                for _ in range(80):
                    time.sleep(.25)
                    try:
                        if window.evaluate_js('document.documentElement.dataset.ready') == 'true':
                            write(args.data_dir / 'smoke-result.json', {'ok': True, 'ui': 'pywebview', 'tasks': len(runtime.snapshot()['tasks'])})
                            break
                    except Exception:
                        pass
                window.destroy()
            window.events.loaded += lambda: threading.Thread(target=smoke, daemon=True).start()
        webview.start(gui='edgechromium' if os.name == 'nt' else None)
        return 0 if not args.smoke_test or (args.data_dir / 'smoke-result.json').exists() else 2
    finally:
        if server:
            server.close()
        if runtime:
            runtime.close()
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
