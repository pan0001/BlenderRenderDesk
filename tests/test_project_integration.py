"""Real Blender validation of scanning, preserving settings, recovery and preview."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
from renderdesk.runtime import Runtime
from renderdesk.engine.protocol import read, digest, frame_file
from renderdesk.projects import scan_project
from renderdesk.processes import installations


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--blender',default=(installations() or ['blender'])[0]);args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='renderdesk-v31-'))
    blend=root/'1200帧 工程.blend';setup=root/'fixture.py'
    setup.write_text('''import bpy,sys
s=bpy.context.scene
s.frame_start=1;s.frame_end=1200;s.frame_step=1
s.render.engine='CYCLES';s.cycles.samples=24
s.render.resolution_x=96;s.render.resolution_y=64;s.render.resolution_percentage=50
s.render.threads_mode='FIXED';s.render.threads=1
s.render.image_settings.file_format='PNG';s.render.image_settings.color_depth='16';s.render.image_settings.color_mode='RGBA'
s.render.filepath='//原始输出/shot_####';s.render.use_file_extension=False
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
''',encoding='utf8')
    subprocess.run([args.blender,'-b','--factory-startup','--python',str(setup),'--',str(blend)],check=True,stdout=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    before=digest(blend);runtime=Runtime(root/'data')
    def wait(jid,condition,timeout=60):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            task=next(t for t in runtime.snapshot()['tasks'] if t['job']['id']==jid)
            if condition(task['status']):return task
            if task['status']['state']=='error':raise AssertionError((root/'data/jobs'/jid/'render.log').read_text(encoding='utf8',errors='replace'))
            time.sleep(.1)
        raise AssertionError(str(runtime.snapshot()))
    try:
        registered=runtime.submit('blender.save',{'path':args.blender,'make_default':True}).result(30)
        scan=runtime.submit('project.scan',{'path':str(blend)}).result(180)
        assert scan['scenes'][0]['total']==1200
        assert scan['scenes'][0]['format']=='PNG' and scan['scenes'][0]['color_depth']=='16'
        jid=runtime.submit('project.import',{'scan_id':scan['scan_id']}).result(10)
        task=wait(jid,lambda s:s['total']==1200);assert task['job']['output']==str(root/'原始输出')
        print('PASS scan: actual 1–1200 project range, PNG16 RGBA, original path, no extension',flush=True)
        small=runtime.submit('project.import',{'scan_id':scan['scan_id'],'overrides':{'range':{'start':1,'end':2,'step':1}}}).result(10)
        runtime.submit('watchdog.save',{'files':True,'recover':True,'attempts':1,'delay':5}).result(10)
        runtime.submit('start',{'id':small}).result(10)
        # Crash only our own spawned child; the watchdog must create a new PID.
        child=runtime.controller.children[small];oldpid=child.pid;child.kill();child.wait(10)
        resumed=wait(small,lambda s:s.get('recovery',{}).get('attempts')==1,timeout=30)
        finished=wait(small,lambda s:s['state']=='complete' and not s['running'])
        assert finished['status']['done']==2 and finished['job']['project']['total']==1200
        launch=read(root/'data/jobs'/small/'launch.json');assert launch['pid']!=oldpid
        for f in [1,2]:
            path=root/'原始输出'/f'shot_{f:04d}'
            data=path.read_bytes();assert data[24]==16 and data[25]==6 # IHDR bit depth, RGBA
            assert int.from_bytes(data[16:20],'big')==48 and int.from_bytes(data[20:24],'big')==32
        preview=runtime.previews.get(small);assert preview.exists()
        assert digest(blend)==before
        print('PASS real crash recovery: new PID, 2/2 task and 1200 project total; PNG16 RGBA 48x32, exact original filename; preview and unchanged .blend',flush=True)
        # Reset a completed real render, preserve its images in the archive and render again.
        reset=runtime.submit('queue.reset',{'id':small,'mode':'rerender'}).result(10)
        assert reset['archived']==2
        fresh=wait(small,lambda s:s['state']=='ready' and s['done']==0)
        assert len(list((root/'原始输出/.renderdesk-history').rglob('shot_*')))==2
        runtime.submit('start',{'id':small}).result(10)
        wait(small,lambda s:s['state']=='complete' and not s['running'])
        assert digest(blend)==before
        print('PASS real completed reset: archived 2 originals, progress 0, rendered 2 frames again',flush=True)
        # Reopen: clear completed queue entries, preserving waiting tasks and files.
        runtime.close();runtime=Runtime(root/'data')
        assert [t['job']['id'] for t in runtime.snapshot()['tasks']]==[jid]
        assert (root/'原始输出/shot_0002').exists()
        assert (root/'data/jobs'/small/'render.log').exists()
        (root/'result.json').write_text(json.dumps({'ok':True,'blend':str(blend),'data':str(root/'data'),'preview':str(preview)},ensure_ascii=False),encoding='utf8')
        print('PASS reopen removes completed queue entry and preserves waiting task, images and logs. Workspace:',root,flush=True)
    finally:
        for child in list(runtime.controller.children.values()):
            if child.poll() is None:child.terminate();child.wait(15)
        runtime.close()

if __name__=='__main__':main()
