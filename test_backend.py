import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from backend import BackgroundManager, LogStream
from core import Manager, scan_processes


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = Manager(self.tmp.name)
        self.folder = self.manager.directory('test')
        self.folder.mkdir(parents=True)
        self.path = self.folder/'render.log'

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_append_split_utf8_and_truncation(self):
        stream = LogStream()
        self.path.write_bytes(b'first\n')
        self.assertEqual(stream.read(self.manager,'test')['text'],'first\n')
        self.assertIsNone(stream.read(self.manager,'test'))
        char = '帧'.encode('utf-8')
        with self.path.open('ab') as f: f.write(char[:2])
        self.assertIsNone(stream.read(self.manager,'test'))
        with self.path.open('ab') as f: f.write(char[2:]+b'\n')
        chunk = stream.read(self.manager,'test')
        self.assertFalse(chunk['reset'])
        self.assertEqual(chunk['text'],'帧\n')
        self.path.write_text('new',encoding='utf-8')
        chunk = stream.read(self.manager,'test')
        self.assertTrue(chunk['reset'])
        self.assertEqual(chunk['text'],'new')

    def test_large_log_catchup_is_bounded_and_selection_resets(self):
        self.path.write_bytes(('渲染日志\n'*100000).encode('utf-8'))
        stream = LogStream()
        chunk = stream.read(self.manager,'test',limit=4096)
        self.assertLessEqual(len(chunk['text'].encode('utf-8')),4096)
        self.assertNotIn('\ufffd',chunk['text'])
        self.assertTrue(chunk['reset'])
        self.assertIsNone(stream.read(self.manager,'test'))
        stream.read(self.manager,None)
        self.assertTrue(stream.read(self.manager,'test')['reset'])

    def test_blocked_poll_does_not_block_ui_or_duplicate_poll(self):
        entered = threading.Event()
        release = threading.Event()
        view = BackgroundManager(self.manager)
        main_thread = threading.get_ident()
        order = []
        def slow_tick():
            self.assertNotEqual(threading.get_ident(),main_thread)
            order.append('tick')
            entered.set()
            release.wait(3)
            return []
        try:
            with patch.object(self.manager,'tick',side_effect=slow_tick):
                view.request_refresh()
                self.assertTrue(entered.wait(1))
                for _ in range(20): view.request_refresh()
                # Cached UI reads are safe even while the worker is blocked.
                self.assertEqual(view.status('test')['done'],0)
                view.submit(lambda manager:order.append('command'))
                release.set()
                kind,snapshot = view.messages.get(timeout=2)
                self.assertEqual(kind,'backend_snapshot')
                view.apply(snapshot)
                kind,payload = view.messages.get(timeout=2)
                self.assertEqual(kind,'backend_operation')
                self.assertIsNone(payload[2])
                self.assertEqual(order,['tick','command'])
        finally:
            release.set()
            view.close()
            view.executor.shutdown(wait=True)

    def test_scan_never_queries_non_blender_command_lines(self):
        class OtherProcess:
            info = {'name':'unrelated.exe'}
            def as_dict(self,*args):
                raise AssertionError('unrelated process details were queried')
        with patch('core.psutil.process_iter',return_value=[OtherProcess()]) as processes:
            self.assertEqual(scan_processes(),[])
            processes.assert_called_once_with(['name'])

if __name__ == '__main__': unittest.main()
