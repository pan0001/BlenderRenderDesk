"""Single-writer SQLite job catalogue; workers use the versioned disk protocol."""
import json
import sqlite3
from pathlib import Path
from .engine.protocol import read


class Catalogue:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'renderdesk-v2.sqlite3', timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, config TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('PRAGMA user_version=2')
        self.db.commit()
        self.issues = []
        # v1 remains readable. Import only new IDs; never rewrite running job files.
        for path in (self.root / 'jobs').glob('*/job.json'):
            try:
                job = read(path)
                if job['id'] != path.parent.name or not all(k in job for k in ('blend', 'output', 'start', 'end', 'step')):
                    raise ValueError('任务字段不完整')
                self.db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?)', (job['id'], json.dumps(job, ensure_ascii=False)))
            except (OSError, ValueError, KeyError, TypeError) as error:
                self.issues.append(f'{path.name}: {error}')
        self.db.commit()

    def jobs(self):
        return {key: json.loads(data) for key, data in self.db.execute('SELECT id,config FROM jobs ORDER BY rowid')}

    def save(self, job):
        with self.db:
            self.db.execute('INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',
                            (job['id'], json.dumps(job, ensure_ascii=False)))

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def close(self):
        self.db.close()
