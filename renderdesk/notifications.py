"""Persistent completion outbox. Network IO never runs on the render controller."""
import json
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from .engine.protocol import read, write


def validate_url(value):
    value = str(value).strip().rstrip('/')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('请输入 Bark 推送地址，例如 https://api.day.app/你的Key')
    if not parsed.path.strip('/'):
        raise ValueError('推送地址必须包含设备 Key')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send_bark(url, title, body):
    # POST JSON keeps titles and paths out of URL/server access logs.
    data = json.dumps({'title': title, 'body': body, 'group': 'Blender Render Desk'}, ensure_ascii=False).encode()
    request = urllib.request.Request(validate_url(url), data=data, headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.build_opener(NoRedirect).open(request, timeout=8) as response:
        result = json.loads(response.read(65536))
        if result.get('code') != 200:
            raise ValueError('Bark 服务未确认接收')


class Notifications:
    def __init__(self, root, sender=send_bark):
        self.root, self.sender = root, sender
        self.path = root / 'bark.json'
        self.lock = threading.Lock()
        self.config = read(self.path, {'enabled': False, 'url': ''})
        self.stop = threading.Event()
        self.db = sqlite3.connect(root / 'notifications.sqlite3')
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, state TEXT);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, title TEXT, body TEXT,
                attempts INTEGER DEFAULT 0, due REAL DEFAULT 0, status TEXT DEFAULT 'pending');''')
        self.thread = threading.Thread(target=self._worker, name='Bark sender', daemon=True)
        self.thread.start()

    def settings(self):
        with self.lock:
            config = dict(self.config)
        row = self.db.execute('SELECT status,attempts FROM outbox ORDER BY id DESC LIMIT 1').fetchone()
        return {'enabled': bool(config.get('enabled')), 'configured': bool(config.get('url')),
                'last_status': row[0] if row else None, 'attempts': row[1] if row else 0}

    def configure(self, values):
        with self.lock:
            config = dict(self.config)
            if values.get('clear'):
                config['url'] = ''
            elif values.get('url'):
                config['url'] = validate_url(values['url'])
            config['enabled'] = bool(values.get('enabled', False))
            if config['enabled'] and not config.get('url'):
                raise ValueError('请先填写 Bark 推送地址')
            write(self.path, config)
            self.config = config
        return self.settings()

    def enqueue(self, title, body):
        self.db.execute('INSERT INTO outbox(title,body) VALUES (?,?)', (title, body))

    def test(self):
        if not self.settings()['enabled']:
            raise ValueError('请先保存并启用 Bark')
        with self.db:
            self.enqueue('Render Desk · 测试通知', '通知连接成功。渲染完成后会在这里提醒你。')

    def observe(self, snapshot):
        enabled = self.settings()['enabled']
        with self.db:
            for task in snapshot['tasks']:
                job, state = task['job'], task['status']
                previous = self.db.execute('SELECT state FROM seen WHERE id=?', (job['id'],)).fetchone()
                current = state['state']
                # Baseline existing finished tasks on first import; no historical notification flood.
                if previous and previous[0] != 'complete' and current == 'complete' and enabled:
                    project=job.get('project',{})
                    partial=bool(job.get('external') and not job.get('batch') and project and
                                 any(job.get(k)!=project.get(k) for k in ('start','end','step')))
                    self.enqueue(('本批渲染完成 · ' if partial else '渲染完成 · ') + job['name'],
                                 f"{state['done']} / {state['total']} 帧已完成\n累计用时 {int(state.get('elapsed', 0))} 秒")
                if not previous or previous[0] != current:
                    self.db.execute('INSERT OR REPLACE INTO seen VALUES (?,?)', (job['id'], current))

    def _worker(self):
        db = sqlite3.connect(self.root / 'notifications.sqlite3', timeout=5)
        try:
            while not self.stop.wait(.5):
                with self.lock:
                    config = dict(self.config)
                if not config.get('enabled') or not config.get('url'):
                    continue
                row = db.execute("SELECT id,title,body,attempts FROM outbox WHERE status='pending' AND due<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
                if not row:
                    continue
                ident, title, body, attempts = row
                try:
                    self.sender(config['url'], title, body)
                    status = 'sent'
                except Exception:
                    # Do not persist exception strings: HTTP errors can contain the secret URL.
                    status = 'failed' if attempts >= 2 else 'pending'
                with db:
                    db.execute('UPDATE outbox SET status=?,attempts=?,due=? WHERE id=?',
                               (status, attempts + 1, time.time() + 15 * 2 ** attempts, ident))
        finally:
            db.close()

    def close(self):
        self.stop.set()
        self.thread.join(10)
        self.db.close()
