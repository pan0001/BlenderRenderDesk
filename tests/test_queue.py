"""Queue operations must preserve images, survive restart and respect live processes."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from renderdesk.service import Controller
from renderdesk.runtime import Runtime
from renderdesk.engine.protocol import write, read, frame_file, receipt, digest
from renderdesk.projects import scene_values
from tests.test_features import sample


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.scan=sample(self.root);self.c=Controller(self.root/'data')
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def add(self,name='任务'):
        jid=self.c.add_project(scene_values(self.scan,overrides={'range':{'start':1,'end':2,'step':1}}))
        self.c.edit(jid,{'name':name});return jid
    def finish(self,jid,complete=False):
        done={}
        for f in ([1,2] if complete else [1]):
            p=frame_file(self.c.jobs[jid],f);p.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(8,8),'blue').save(p);done[str(f)]=receipt(p)
        write(self.c.directory(jid)/'progress.json',{'done':done})
        write(self.c.directory(jid)/'status.json',{'state':'complete' if complete else 'paused','elapsed_total':43})
        return done
    def reopen(self):
        self.c.close();self.c=Controller(self.root/'data')

    def test_edit_validation_and_persistence(self):
        jid=self.add();self.c.edit(jid,{'name':'夜景','start':2,'end':2,'step':1,'threads':2,'output':str(self.root/'new')})
        self.reopen();j=self.c.jobs[jid]
        self.assertEqual(j['name'],'夜景');self.assertEqual(j['start'],2)
        self.assertEqual(frame_file(j,2),self.root/'new/shot_0002.png')
        with self.assertRaises(ValueError):self.c.edit(jid,{'end':1201})
        with self.assertRaises(ValueError):self.c.edit(jid,{'name':''})
    def test_partial_task_name_only_until_reset(self):
        jid=self.add();self.finish(jid)
        self.c.edit(jid,{'name':'保留图片'})
        with self.assertRaisesRegex(ValueError,'已有完成帧'):self.c.edit(jid,{'threads':4})
        self.assertEqual(self.c.status(jid)['done'],1)
    def test_reorder_persist_and_reject_stale_client(self):
        a,b,c=self.add('A'),self.add('B'),self.add('C')
        self.c.reorder([c,a,b]);self.reopen()
        self.assertEqual([t['job']['id'] for t in self.c.snapshot()['tasks']],[c,a,b])
        for ids in ([a,b],[a,a,c],[a,b,'unknown']):
            with self.assertRaises(ValueError):self.c.reorder(ids)
    def test_remove_keeps_outputs_and_never_reimports(self):
        jid=self.add();done=self.finish(jid);p=Path(done['1']['path']);sha=digest(p)
        self.c.request_change(jid,'remove');self.assertNotIn(jid,self.c.jobs)
        self.assertEqual(digest(p),sha);self.assertTrue((self.c.root/'jobs'/jid/'job.json').exists())
        self.reopen();self.assertNotIn(jid,self.c.jobs)
    def test_reset_partial_and_complete_archive_frames(self):
        for complete in (False,True):
            jid=self.add();records=self.finish(jid,complete)
            result=self.c.request_change(jid,'reset')
            self.assertEqual(result['archived'],len(records))
            self.assertEqual(self.c.status(jid)['state'],'ready');self.assertEqual(self.c.status(jid)['done'],0)
            self.assertEqual(self.c.status(jid)['elapsed'],0)
            reset=self.c.jobs[jid]['last_reset']
            for r in records.values():
                original=Path(r['path']);archive=original.parent/'.renderdesk-history'/jid/reset/original.name
                self.assertFalse(original.exists());self.assertEqual(digest(archive),r['sha256'])
            self.c.request_change(jid,'remove')
    def test_keep_reset_preserves_progress_and_clears_plan(self):
        jid=self.add();done=self.finish(jid)
        self.c.schedule(jid,9999999999,10000000000)
        self.c.request_change(jid,'reset','keep')
        self.assertEqual(self.c.status(jid)['done'],1);self.assertEqual(self.c.status(jid)['state'],'ready')
        self.assertIsNone(self.c.jobs[jid]['start_at']);self.assertIsNone(self.c.jobs[jid]['pause_at'])
        self.assertTrue(Path(done['1']['path']).exists())
    def test_running_reset_and_delete_wait_for_exit(self):
        for action in ('reset','remove'):
            jid=self.add();self.finish(jid)
            with patch.object(self.c,'live',return_value=True):
                self.assertTrue(self.c.request_change(jid,action)['pending'])
                self.assertTrue(read(self.c.directory(jid)/'control.json')['pause'])
                self.c.process_changes();self.assertIn(jid,self.c.jobs)
                with self.assertRaises(ValueError):self.c.edit(jid,{'name':'x'})
                with self.assertRaises(ValueError):self.c.start(jid)
            self.c.process_changes()
            if action=='remove':self.assertNotIn(jid,self.c.jobs)
            else:self.assertEqual(self.c.status(jid)['done'],0)
    def test_reset_journal_resumes_after_partial_move(self):
        jid=self.add();records=self.finish(jid,True)
        real=Path.rename;count=[0]
        def move(path,target):
            count[0]+=1
            if count[0]==2:raise PermissionError('simulated file lock')
            return real(path,target)
        with patch.object(Path,'rename',move):
            with self.assertRaises(PermissionError):self.c.request_change(jid,'reset')
        self.reopen();self.c.request_change(jid,'reset')
        self.assertEqual(self.c.status(jid)['done'],0)
        for r in records.values():self.assertFalse(Path(r['path']).exists())
        self.assertEqual(len(list((self.root/'images/.renderdesk-history').rglob('*.png'))),2)
    def test_reset_rejects_modified_or_foreign_records(self):
        jid=self.add();records=self.finish(jid);p=Path(records['1']['path']);p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'发生变化'):self.c.request_change(jid,'reset')
        self.assertEqual(p.read_bytes(),b'changed');self.assertEqual(self.c.status(jid)['done'],1)
    def test_startup_cleanup_only_completed_stopped_tasks(self):
        complete,partial,ready=self.add('complete'),self.add('partial'),self.add('ready')
        records=self.finish(complete,True)
        # Separate output for the partial task.
        self.c.edit(partial,{'output':str(self.root/'partial')});self.finish(partial)
        self.c.close();runtime=Runtime(self.root/'data')
        try:
            ids={t['job']['id'] for t in runtime.snapshot()['tasks']}
            self.assertNotIn(complete,ids);self.assertIn(partial,ids);self.assertIn(ready,ids)
            self.assertTrue(Path(records['1']['path']).exists())
        finally:runtime.close();self.c=Controller(self.root/'data')
        self.assertNotIn(complete,self.c.jobs)
    def test_cleanup_does_not_remove_still_exiting_process(self):
        jid=self.add();self.finish(jid,True)
        with patch.object(self.c,'live',return_value=True):self.assertEqual(self.c.cleanup_completed(),0)
        self.assertIn(jid,self.c.jobs)

    def test_external_last_frame_is_observed_before_reset(self):
        jid=self.add();j=self.c.jobs[jid]
        j['external']={'phase':'attached','pattern':'####.png'};self.c.save(j)
        p=frame_file(j,1);p.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(8,8),'green').save(p)
        # No observer has written a receipt yet; the original process just exited.
        result=self.c.request_change(jid,'reset')
        self.assertEqual(result['archived'],1)
        self.assertFalse(p.exists());self.assertEqual(self.c.status(jid)['done'],0)

    def test_startup_cleans_external_completed_while_manager_closed(self):
        jid=self.add();j=self.c.jobs[jid]
        j['external']={'phase':'attached','pattern':'####.png'};self.c.save(j)
        for f in (1,2):
            p=frame_file(j,f);p.parent.mkdir(parents=True,exist_ok=True)
            Image.new('RGB',(8,8),'green').save(p)
        write(self.c.directory(jid)/'status.json',{'state':'rendering'})
        self.c.close();runtime=Runtime(self.root/'data')
        try:self.assertEqual(runtime.snapshot()['tasks'],[])
        finally:runtime.close();self.c=Controller(self.root/'data')
        self.assertTrue(p.exists())

if __name__=='__main__':unittest.main()
