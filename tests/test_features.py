"""Regression checks for project settings, previews, token rotation and recovery."""
import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image
from renderdesk.engine.protocol import write, signature, receipt, frame_file
from renderdesk.projects import scene_values
from renderdesk.service import Controller
from renderdesk.previews import Previews
from renderdesk.watch import Watchdog
from renderdesk.runtime import Runtime
from renderdesk.server import create_app


def sample(root):
    blend, exe = root/'scene.blend', root/'blender.exe'
    blend.touch(); exe.touch()
    paths={str(f):str(root/'images'/f'shot_{f:04d}.png') for f in range(1,1201)}
    scene={'scene':'Scene','camera':'Camera','start':1,'end':1200,'step':1,'total':1200,'format':'PNG',
           'color_depth':'16','color_mode':'RGBA','engine':'CYCLES','resolution_x':1920,'resolution_y':1080,
           'resolution_percentage':50,'fps':24,'filepath':'//images/shot_####','use_file_extension':True,
           'use_multiview':False,'threads':4,'project_paths':paths,'first_output':paths['1']}
    return {'blend':str(blend),'blender':str(exe),'source':signature(blend),'active_scene':'Scene','scenes':[scene]}


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.scan=sample(self.root)
        self.c=Controller(self.root/'data')
    def tearDown(self):
        self.c.close(); self.temp.cleanup()

    def test_preserve_settings_and_paths(self):
        values=scene_values(self.scan)
        jid=self.c.add_project(values)
        j=self.c.jobs[jid]
        self.assertEqual(self.c.status(jid)['total'],1200)
        self.assertEqual(frame_file(j,1200),self.root/'images/shot_1200.png')
        self.assertEqual(j['format'],'PNG');self.assertEqual(j['color_depth'],'16')
        self.assertEqual(j['output'],str(self.root/'images'));self.assertTrue(j['preserve_project'])
        self.assertNotIn('project_paths',self.c.snapshot()['tasks'][0]['job'])

    def test_explicit_override_keeps_project_total(self):
        values=scene_values(self.scan,overrides={'range':{'start':2,'end':6,'step':2},'output':str(self.root/'custom')})
        self.assertEqual(values['project']['total'],1200)
        self.assertEqual(list(values['project_paths']),['2','4','6'])
        self.assertEqual(frame_file(values,2),self.root/'custom/shot_0002.png')
        with self.assertRaises(ValueError):scene_values(self.scan,overrides={'range':{'start':1,'end':1201,'step':1}})

    def test_legacy_60_rescan_to_1200_preserves_receipts(self):
        jid=self.c.add({**scene_values(self.scan), 'end':60})
        # A legacy task uses the old frame filename convention.
        job=self.c.jobs[jid];job.pop('project_paths');job.pop('preserve_project');self.c.save(job)
        path=frame_file(job,1);Image.new('RGB',(4,4)).save(path)
        write(self.c.directory(jid)/'progress.json',{'done':{'1':receipt(path)}})
        write(self.c.directory(jid)/'status.json',{'state':'complete'})
        self.c.rescan(jid,scene_values(self.scan))
        self.assertEqual(self.c.status(jid)['total'],1200)
        self.assertEqual(self.c.status(jid)['done'],1)
        self.assertEqual(self.c.status(jid)['state'],'interrupted')
        self.assertTrue(path.exists())

    def test_preview_only_completed_frame_and_file_integrity(self):
        jid=self.c.add_project(scene_values(self.scan))
        path=frame_file(self.c.jobs[jid],7);path.parent.mkdir();Image.new('RGB',(2000,1000),'red').save(path)
        write(self.c.directory(jid)/'progress.json',{'done':{'7':receipt(path)}})
        previews=Previews(self.c.root);target=previews.get(jid)
        with Image.open(target) as img:self.assertEqual(img.size,(960,480))
        path.write_bytes(b'corrupted')
        with self.assertRaises(ValueError):previews.get(jid)
        with self.assertRaises(ValueError):previews.get('../secret')

    def test_movie_output_is_not_silently_converted(self):
        self.scan['scenes'][0]['format']='FFMPEG'
        with self.assertRaisesRegex(ValueError,'视频'):scene_values(self.scan)

    def test_watchdog_event_and_crash_recovery_bound(self):
        jid=self.c.add_project(scene_values(self.scan));w=Watchdog(self.c.root)
        try:
            w.configure({'files':True,'recover':True,'attempts':1,'delay':5})
            w.sync(self.c.jobs)
            write(self.c.directory(jid)/'status.json',{'state':'rendering'})
            self.assertTrue(w.changed.wait(5))
            def snap(running,state):return {'tasks':[{'job':self.c.jobs[jid], 'status':{'running':running,'state':state}}]}
            w.observe(snap(True,'rendering'),self.c)
            w.observe(snap(False,'interrupted'),self.c)
            self.assertIn('due',w.history[jid]);w.history[jid]['due']=0.1
            with patch.object(self.c,'start') as start:
                w.observe(snap(False,'interrupted'),self.c)
                start.assert_called_once_with(jid)
                w.observe(snap(False,'interrupted'),self.c)
                self.assertNotIn('due',w.history[jid])
            w.reset(jid);w.observe(snap(True,'rendering'),self.c)
            w.observe(snap(False,'paused'),self.c)
            self.assertNotIn('due',w.history[jid])
        finally:w.close()

    def test_api_rotation_and_heavy_scan_do_not_block_state(self):
        runtime=Runtime(self.root/'web')
        try:
            app=create_app(runtime,'old-key');client=app.test_client()
            h={'Authorization':'Bearer old-key'}
            self.assertEqual(client.post('/api/access',headers=h,json={'token':'short'}).status_code,400)
            r=client.post('/api/access',headers=h,json={'token':'manual-new-key-1234'})
            self.assertEqual(r.status_code,200)
            self.assertEqual(client.get('/api/state',headers=h).status_code,401)
            h={'Authorization':'Bearer manual-new-key-1234'}
            self.assertEqual(client.get('/api/state',headers=h).status_code,200)
            runtime.registry.resolve=lambda _:str(self.root/'blender.exe')
            with patch('renderdesk.runtime.scan_project',side_effect=lambda *a:(time.sleep(.5),self.scan)[1]):
                pending=runtime.submit('project.scan',{'path':str(self.root/'scene.blend')})
                start=time.monotonic();runtime.submit('settings',{}).result(1)
                self.assertLess(time.monotonic()-start,.4)
                scan=pending.result(2)
                self.assertNotIn('project_paths',scan['scenes'][0])
        finally:runtime.close()

if __name__=='__main__':unittest.main()
