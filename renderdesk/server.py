"""Authenticated HTTP API and static UI, served by Waitress in both modes."""
from concurrent.futures import TimeoutError
import hmac
from pathlib import Path
import queue
import secrets
import threading
import time
from urllib.parse import urlsplit
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException
from .engine.protocol import read, write


def create_app(runtime, token, remote=False):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 65536
    web = Path(__file__).parent / 'web'
    operations, operation_lock = {}, threading.Lock()
    access_key = {'token': token}

    @app.before_request
    def authorize():
        if not request.path.startswith('/api/'):
            return None
        # API uses explicit bearer credentials, never cookies. Cross-site requests are rejected.
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return jsonify(error='不允许跨站请求'), 403
        auth = request.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode('utf8'), ('Bearer ' + access_key['token']).encode('utf8')):
            return jsonify(error='请填写访问密钥'), 401

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(Exception)
    def error(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        if isinstance(error, (ValueError, KeyError, TypeError)):
            return jsonify(error=str(error)), 400
        return jsonify(error='服务暂时无法处理请求，请查看任务日志后重试'), 500

    @app.get('/')
    def index():
        return send_from_directory(web, 'index.html')

    @app.get('/assets/<name>')
    def assets(name):
        if name not in ('app.js', 'style.css'):
            return '', 404
        return send_from_directory(web, name)

    @app.get('/api/state')
    def state():
        return jsonify(runtime.snapshot())

    @app.get('/api/settings')
    def settings():
        result = runtime.submit('settings', {}).result(10)
        config = read(runtime.root / 'server.json', {})
        result.update(remote=remote, remote_next=bool(config.get('remote')), port=config.get('port', 8765))
        return jsonify(result)

    @app.post('/api/remote')
    def remote_settings():
        values = request.get_json()
        port = int(values.get('port', 8765))
        if not 1024 <= port <= 65535:
            raise ValueError('端口需在 1024–65535 之间')
        write(runtime.root / 'server.json', {'remote': bool(values.get('enabled')), 'port': port})
        return jsonify(ok=True, message='已保存，下次启动程序生效')

    @app.get('/api/access')
    def access():
        return jsonify(token=access_key['token'])

    @app.post('/api/access')
    def change_access():
        new_key = str(request.get_json().get('token', ''))
        if not 16 <= len(new_key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in new_key):
            raise ValueError('访问密钥需为 16–128 位英文字母、数字或符号，不能含空格')
        write(runtime.root / 'access-token.json', {'token': new_key})
        access_key['token'] = new_key
        return jsonify(token=new_key)

    @app.get('/api/jobs/<jid>/preview')
    def preview(jid):
        if jid not in {t['job']['id'] for t in runtime.snapshot()['tasks']}:
            raise ValueError('找不到任务')
        return send_file(runtime.previews.get(jid), mimetype='image/jpeg')

    @app.post('/api/commands')
    def command():
        body = request.get_json()
        if not isinstance(body, dict) or body.get('action') not in ('add', 'attach', 'start', 'pause', 'schedule', 'bark.save', 'bark.test', 'refresh', 'project.scan', 'project.import', 'project.rescan', 'blender.save', 'watchdog.save', 'frpc.save', 'frpc.start', 'frpc.stop'):
            raise ValueError('无效操作')
        values = body.get('values', {})
        if not isinstance(values, dict):
            raise ValueError('无效参数')
        with operation_lock:
            for key, (future, stamp) in list(operations.items()):
                if future.done() and time.time() - stamp > 300:
                    del operations[key]
            if len(operations) >= 128:
                return jsonify(error='待处理操作过多，请稍后重试'), 429
            try:
                future = runtime.submit(body['action'], values)
            except queue.Full:
                return jsonify(error='任务繁忙，请稍后重试'), 429
            ident = secrets.token_hex(12)
            operations[ident] = (future, time.time())
        return jsonify(operation=ident), 202

    @app.get('/api/operations/<ident>')
    def operation(ident):
        with operation_lock:
            pair = operations.get(ident)
        if not pair:
            return jsonify(error='操作记录已过期，请刷新任务状态'), 404
        future = pair[0]
        if not future.done():
            return jsonify(done=False)
        try:
            return jsonify(done=True, result=future.result())
        except Exception as error:
            # Service errors contain useful local file validation, but Bark network errors never reach here.
            return jsonify(done=True, error=str(error))

    @app.get('/api/jobs/<jid>/log')
    def log(jid):
        return jsonify(runtime.log(jid, int(request.args.get('cursor', -1))))

    return app
