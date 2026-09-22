import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib
from renderdesk.engine.protocol import digest, frame_file, png_complete, read, receipt, verify, write
from renderdesk.service import Controller, LogReader
from renderdesk.storage import Catalogue


def png_bytes():
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b'')


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.blend = self.root / '中文 场景.blend'
        self.blend.write_bytes(b'fixture')
        self.exe = self.root / 'blender.exe'
        self.exe.write_bytes(b'fixture')
        self.controller = Controller(self.root / 'state')

    def tearDown(self):
        self.controller.close()
        self.temp.cleanup()

    def add(self, **extra):
        return self.controller.add({'blend': str(self.blend), 'blender': str(self.exe), 'start': 1, 'end': 5, 'step': 1, 'output': str(self.root / 'images'), **extra})

    def test_catalogue_survives_restart(self):
        jid = self.add()
        self.controller.close()
        self.controller = Controller(self.root / 'state')
        self.assertIn(jid, self.controller.jobs)
        self.assertEqual(self.controller.status(jid)['state'], 'ready')

    def test_v1_import_does_not_rewrite_job(self):
        legacy = self.root / 'legacy'
        path = legacy / 'jobs' / 'old123' / 'job.json'
        job = {'id': 'old123', 'blend': str(self.blend), 'output': str(self.root), 'start': 1, 'end': 2, 'step': 1}
        write(path, job)
        before = path.read_bytes()
        db = Catalogue(legacy)
        self.assertEqual(db.jobs()['old123'], job)
        self.assertEqual(path.read_bytes(), before)
        db.close()

    def test_invalid_migration_is_reported_and_preserved(self):
        path = self.root / 'legacy/jobs/broken/job.json'
        path.parent.mkdir(parents=True)
        path.write_text('{broken')
        db = Catalogue(self.root / 'legacy')
        self.assertTrue(db.issues)
        self.assertEqual(path.read_text(), '{broken')
        db.close()

    def test_negative_frame_filename(self):
        self.assertEqual(frame_file({'output': '.', 'external': {'pattern': 'shot_####.png'}}, -2).name, 'shot_-002.png')

    def test_unique_output_directories(self):
        a, b = self.add(), self.add()
        self.assertNotEqual(self.controller.jobs[a]['output'], self.controller.jobs[b]['output'])

    def test_invalid_ranges_rejected(self):
        for values in ({'step': 0}, {'start': 7, 'end': 2}, {'end': 200000}, {'threads': -1}):
            with self.assertRaises(ValueError):
                self.add(**values)

    def test_changed_blend_rejected(self):
        jid = self.add()
        self.blend.write_bytes(b'new fixture')
        with self.assertRaisesRegex(ValueError, '工程已修改'):
            self.controller.start(jid)

    def test_expired_schedule_never_starts(self):
        jid = self.add()
        self.controller.schedule(jid, time.time() + 10, time.time() + 20)
        with patch.object(self.controller, 'start') as start:
            self.controller.tick(time.time() + 30)
            start.assert_not_called()
        self.assertIsNone(self.controller.jobs[jid]['start_at'])

    def test_schedule_fires_once(self):
        jid = self.add()
        self.controller.schedule(jid, time.time() + 10)
        with patch.object(self.controller, 'start') as start:
            self.controller.tick(time.time() + 12)
            self.controller.tick(time.time() + 13)
            self.assertEqual(start.call_count, 1)

    def test_pause_cancels_schedule(self):
        jid = self.add()
        self.controller.schedule(jid, time.time() + 10, time.time() + 20)
        with patch.object(self.controller, 'live', return_value=True):
            self.controller.pause(jid)
        self.assertTrue(read(self.controller.directory(jid) / 'control.json')['pause'])
        self.assertIsNone(self.controller.jobs[jid]['start_at'])

    def test_crc_and_truncation(self):
        path = self.root / 'image.png'
        valid = png_bytes()
        path.write_bytes(valid)
        self.assertTrue(png_complete(path))
        path.write_bytes(valid[:-1])
        self.assertFalse(png_complete(path))
        path.write_bytes(valid[:20] + bytes([valid[20] ^ 1]) + valid[21:])
        self.assertFalse(png_complete(path))

    def test_same_size_tamper_detected(self):
        path = self.root / 'image.png'
        path.write_bytes(png_bytes())
        data = receipt(path)
        self.assertTrue(verify(data))
        original = path.read_bytes()
        path.write_bytes(b'X' + original[1:])
        with self.assertRaises(ValueError):
            verify(data)

    def test_missing_receipt_file_can_be_repaired(self):
        path = self.root / 'image.png'
        path.write_bytes(png_bytes())
        data = receipt(path)
        path.unlink()
        self.assertFalse(verify(data))

    def test_log_incremental_unicode_and_truncation(self):
        reader = LogReader()
        path = self.root / 'render.log'
        encoded = '日志'.encode()
        path.write_bytes(encoded[:2])
        self.assertEqual(reader.poll(path), (True, ''))
        with path.open('ab') as stream:
            stream.write(encoded[2:])
        self.assertEqual(reader.poll(path), (False, '日志'))
        self.assertEqual(reader.poll(path), (False, ''))
        path.write_bytes(b'x')
        self.assertEqual(reader.poll(path), (True, 'x'))

    def test_log_initial_tail_is_bounded(self):
        path = self.root / 'render.log'
        path.write_bytes(b'x' * 2000000)
        self.assertEqual(len(LogReader().poll(path)[1]), 32768)

    def test_stale_pid_not_running(self):
        import os
        import psutil
        from renderdesk.processes import process
        self.assertIsNone(process({'pid': os.getpid(), 'created': psutil.Process().create_time() - 100}))


if __name__ == '__main__':
    unittest.main()
