import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from core import Manager, atomic_json, read_json, alive

class DurableJobsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.blend = self.root / '中文 工程.blend'
        self.blend.write_bytes(b'fixture')
        self.exe = self.root / 'blender.exe'
        self.exe.write_bytes(b'fixture')
        self.m = Manager(self.root / 'data')
        self.j = self.m.add(self.blend, self.exe, 1, 5, 2, self.root / 'out')
        self.jid = self.j['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_reopen_preserves_job_and_schedule(self):
        t = time.time() + 30
        self.m.schedule(self.jid, t, t + 60)
        m2 = Manager(self.m.root)
        self.assertEqual(m2.jobs[self.jid]['start_at'], t)
        self.assertEqual(m2.jobs[self.jid]['step'], 2)

    def test_expired_window_does_not_launch(self):
        t = time.time() + 30
        self.m.schedule(self.jid, t, t + 60)
        with patch.object(self.m, 'start') as start:
            self.m.tick(now=t+61)
            start.assert_not_called()
        self.assertIsNone(self.j['start_at'])
        self.assertIsNone(self.j['pause_at'])

    def test_due_start_fires_only_once(self):
        t = time.time() + 30
        self.m.schedule(self.jid, t, None)
        with patch.object(self.m, 'start') as start:
            self.m.tick(now=t+1)
            self.m.tick(now=t+2)
            start.assert_called_once_with(self.jid)

    def test_timed_pause_is_cooperative(self):
        t = time.time() + 30
        self.m.schedule(self.jid, None, t)
        with patch.object(self.m, 'live', return_value=True):
            self.m.tick(now=t+1)
        self.assertTrue(read_json(self.m.directory(self.jid) / 'control.json')['pause'])
        self.assertIsNone(self.j['pause_at'])

    def test_pause_clears_future_restart(self):
        t = time.time() + 30
        self.m.schedule(self.jid, t, t+60)
        with patch.object(self.m, 'live', return_value=True):
            self.m.pause(self.jid)
        self.assertIsNone(self.j['start_at'])

    def test_modified_source_refuses_resume(self):
        self.blend.write_bytes(b'changed file')
        with self.assertRaisesRegex(ValueError, '修改'):
            self.m.start(self.jid)

    def test_pid_reuse_is_not_accepted(self):
        import os
        self.assertFalse(alive({'pid': os.getpid(), 'created': 0}))

    def test_missing_worker_becomes_interrupted(self):
        atomic_json(self.m.directory(self.jid) / 'status.json', {'state': 'rendering', 'frame': 3})
        self.assertEqual(self.m.status(self.jid)['state'], 'interrupted')

    def test_isolated_outputs(self):
        j2 = self.m.add(self.blend, self.exe, 1, 5, 1, self.root / 'out')
        self.assertNotEqual(self.j['output'], j2['output'])

    def test_reversed_schedule_rejected(self):
        t = time.time()+30
        with self.assertRaises(ValueError):
            self.m.schedule(self.jid, t+10, t)

if __name__ == '__main__':
    unittest.main(verbosity=2)
