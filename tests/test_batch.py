import ast
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from .test_features import sample
from .test_adoption import SCRIPT
from renderdesk.batch import infer_batch, command_matches
from renderdesk.service import Controller
from renderdesk.engine.protocol import read, write, signature
from renderdesk.projects import scene_values


PLAN='''from pathlib import Path
import subprocess,time
R=Path(__file__).resolve().parent
blender=BLENDER
for a in range(1,13,4):
 b=min(a+3,12)
 if all((R/'renders/final'/f'shot_{i:04d}.png').exists() for i in range(a,b+1)):continue
 with (R/f'part_{a}.log').open('w') as log:
  r=subprocess.Popen([blender,'-b',str(R/'scene.blend'),'--python',str(R/'render.py'),'--','final',str(a),str(b)],stdout=log,stderr=subprocess.STDOUT)
  while r.poll() is None:time.sleep(.1)
 if r.returncode!=0:raise SystemExit(1)
'''


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.scan=sample(self.root);self.script=self.root/'render.py';self.script.write_text(SCRIPT,encoding='utf8')
        (self.root/'renders/final').mkdir(parents=True)
        self.path=self.root/'plan.py';self.path.write_text(PLAN.replace('BLENDER',repr(self.scan['blender'])),encoding='utf8')
        self.parent=Mock();self.parent.pid=100
        self.parent.exe.return_value='C:/Python/python.exe';self.parent.cwd.return_value=str(self.root)
        self.parent.create_time.return_value=time.time();self.parent.cmdline.return_value=['C:/Python/python.exe',str(self.path)]
        self.child=Mock();self.child.parent.return_value=self.parent
        self.args=[self.scan['blender'],'-b',self.scan['blend'],'--python',str(self.script),'--','final','5','8']
        self.child.cmdline.return_value=self.args
    def tearDown(self):self.tmp.cleanup()
    def test_full_plan_not_current_batch(self):
        plan=infer_batch(self.child,self.scan)
        self.assertEqual((plan['start'],plan['end'],len(plan['batches'])),(1,12,3))
        self.assertEqual(plan['batches'][1]['start'],5)
        self.assertEqual(plan['batches'][1]['log'],str(self.root/'part_5.log'))
    def test_unrelated_command_cannot_be_adopted(self):
        self.child.cmdline.return_value=self.args[:-2]+['20','30']
        with self.assertRaisesRegex(ValueError,'不属于'):infer_batch(self.child,self.scan)
    def test_parent_changed_since_start_rejected(self):
        self.parent.create_time.return_value=1
        with self.assertRaisesRegex(ValueError,'修改'):infer_batch(self.child,self.scan)
    def test_no_restart_without_skip_existing_frames(self):
        self.script.write_text(SCRIPT.replace('    if path.exists(): continue',''),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'跳过'):infer_batch(self.child,self.scan)
    def test_non_python_parent_not_inferred(self):
        self.parent.exe.return_value='C:/Windows/cmd.exe'
        self.assertIsNone(infer_batch(self.child,self.scan))
    def test_module_launch_not_inferred(self):
        self.parent.cmdline.return_value=['python','-m','some.module']
        self.assertIsNone(infer_batch(self.child,self.scan))
    def test_unknown_branch_not_guessed(self):
        self.path.write_text(self.path.read_text().replace(' b=min(a+3,12)',' b=min(a+3,12)\n if runtime_flag: b=99'),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'运行时分支'):infer_batch(self.child,self.scan)
    def test_matching_does_not_ignore_frame_args(self):
        self.assertTrue(command_matches(['C:\\Blender\\blender.exe','--','4'],['C:/Blender/blender.exe','--','4']))
        self.assertFalse(command_matches(['blender','--','4'],['blender','--','5']))
    def test_parallel_scheduler_rejected(self):
        self.path.write_text(self.path.read_text().replace('  while r.poll() is None:time.sleep(.1)','  time.sleep(.1)'),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'并行调度'):infer_batch(self.child,self.scan)

    def test_parent_exit_does_not_complete_partial_plan(self):
        self.assert_exit_state(1,0,'interrupted')

    def test_nonzero_scheduler_exit_is_not_success(self):
        self.assert_exit_state(2,7,'interrupted')

    def test_complete_frames_and_successful_exit(self):
        self.assert_exit_state(2,0,'complete')

    def assert_exit_state(self, count, exit_code, expected):
        c=Controller(self.root/'state-test')
        try:
            jid=c.add_project(scene_values(self.scan,overrides={'range':{'start':1,'end':2,'step':1}}))
            job=c.jobs[jid];job['batch']={'batches':[]};job['external']={'phase':'attached','pattern':'####.png'}
            directory=c.directory(jid)
            write(directory/'batch-launch.json',{'pid':42,'created':1})
            write(directory/'batch-result.json',{'exit_code':exit_code})
            records={str(f):{'path':str(self.root/f'{f:04d}.png'),'mtime_ns':f} for f in range(1,count+1)}
            with patch('renderdesk.batch_control.process',return_value=None),patch.object(c,'inspect_external',return_value=records):
                c.observe_batch(job)
            self.assertEqual(read(directory/'status.json')['state'],expected)
        finally:c.close()


if __name__=='__main__':unittest.main()
