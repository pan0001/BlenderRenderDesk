import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
from PIL import Image
from renderdesk.adoption import infer_script
from renderdesk.runtime import Runtime
from renderdesk.server import create_app
from tests.test_features import sample


SCRIPT = '''import bpy,sys
from pathlib import Path
root=Path(__file__).resolve().parent
args=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
mode=args[0] if args else 'preview'
start=int(args[1]) if len(args)>1 else 1
end=int(args[2]) if len(args)>2 else 1200
s=bpy.data.scenes['Scene'];bpy.context.window.scene=s
s.render.image_settings.file_format='PNG'
if mode=='final': frames=list(range(start,end+1))
else: frames=list(range(start,end+1,2))
out=root/'renders'/mode
out.mkdir(parents=True,exist_ok=True)
for f in frames:
    path=out/f'shot_{f:04d}.png'
    if path.exists(): continue
    s.frame_set(f)
    s.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
'''


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.scan=sample(self.root);self.script=self.root/'render.py'
        self.script.write_text(SCRIPT,encoding='utf8')
        self.argv=[self.scan['blender'],'-b',self.scan['blend'],'--python',str(self.script),'--','final','181','240']
        self.out=self.root/'renders/final';self.out.mkdir(parents=True)
        Image.new('RGB',(8,8)).save(self.out/'shot_0181.png')
    def tearDown(self):self.tmp.cleanup()
    def infer(self):return infer_script(self.script,self.argv,self.root,self.scan)
    def test_actual_script_output_and_cli_subset(self):
        result=self.infer()
        self.assertEqual((result['start'],result['end'],result['step']),(181,240,1))
        self.assertEqual(result['project']['total'],1200)
        self.assertEqual(result['output'],str(self.out));self.assertEqual(result['pattern'],'shot_####.png')
        self.assertEqual(result['range_source'],'script')
    def test_selected_branch_step_and_default_args(self):
        self.argv=self.argv[:self.argv.index('--')]
        (self.root/'renders/preview').mkdir()
        result=self.infer();self.assertEqual((result['start'],result['end'],result['step']),(1,1199,2))
    def test_no_script_import_or_execution(self):
        marker=self.root/'must-not-exist'
        self.script.write_text("import unavailable_dangerous_module\nPath("+repr(str(marker))+").write_text('bad')\n"+SCRIPT,encoding='utf8')
        self.infer();self.assertFalse(marker.exists())
    def test_runtime_png_override_of_saved_movie(self):
        self.scan['scenes'][0]['format']='FFMPEG'
        self.assertEqual(self.infer()['format'],'PNG')
    def test_waiting_first_frame_does_not_require_manual_setup(self):
        (self.out/'shot_0181.png').unlink()
        self.assertIn('等待首帧',self.infer()['detection'])
    def test_unknown_output_is_not_guessed_from_existing_files(self):
        self.script.write_text(SCRIPT.replace("out=root/'renders'/mode","out=custom_output()"),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'无法确定脚本实际输出'):self.infer()
    def test_unknown_render_branch_is_rejected(self):
        self.script.write_text(SCRIPT.replace("if mode=='final':","if runtime_only_flag:"),encoding='utf8')
        with self.assertRaises(ValueError):self.infer()
    def test_missing_real_folder_does_not_fall_back_to_project(self):
        self.script.write_text(SCRIPT.replace("out=root/'renders'/mode","out=root/'does-not-exist'/mode"),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'实际输出目录'):self.infer()
    def test_nonuniform_frame_list_is_rejected(self):
        self.script.write_text(SCRIPT.replace('list(range(start,end+1))','[181,183,190]'),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'固定步长'):self.infer()
    def test_png_extension_and_scene_range(self):
        text=SCRIPT.replace("start=int(args[1]) if len(args)>1 else 1","start=181").replace("frames=list(range(start,end+1))","frames=range(start,s.frame_end+1)").replace("f'shot_{f:04d}.png'","'shot_{:04d}'.format(f)")
        self.script.write_text(text,encoding='utf8')
        self.assertEqual(self.infer()['end'],1200);self.assertEqual(self.infer()['pattern'],'shot_####.png')
    def test_single_api_action_and_duplicate_inflight(self):
        runtime=Runtime(self.root/'runtime')
        entered=threading.Event();release=threading.Event()
        def detect(row):entered.set();release.wait(5);return 'recognized-id'
        try:
            with patch.object(runtime,'_adopt',side_effect=detect) as adopt:
                app=create_app(runtime,'test');client=app.test_client();headers={'Authorization':'Bearer test'}
                values={'process':{'pid':42,'created':123.0}}
                response=client.post('/api/commands',headers=headers,json={'action':'process.adopt','values':values})
                self.assertEqual(response.status_code,202);self.assertTrue(entered.wait(2))
                future=runtime.submit('process.adopt',values)
                self.assertFalse(future.done());self.assertEqual(client.get('/api/state',headers=headers).status_code,200)
                release.set();self.assertEqual(future.result(2),'recognized-id');self.assertEqual(adopt.call_count,1)
                result=client.get('/api/operations/'+response.json['operation'],headers=headers).json
                self.assertEqual(result['result'],'recognized-id')
        finally:release.set();runtime.close()

    def test_shadowed_helpers_are_not_mistaken_for_builtins(self):
        self.script.write_text('def range(*args): return [1,2]\n'+SCRIPT,encoding='utf8')
        with self.assertRaisesRegex(ValueError,'重定义'):self.infer()

    def test_dynamic_execution_is_rejected_without_running(self):
        self.script.write_text("exec('raise RuntimeError()')\n"+SCRIPT,encoding='utf8')
        with self.assertRaisesRegex(ValueError,'动态执行'):self.infer()


if __name__=='__main__':unittest.main()
