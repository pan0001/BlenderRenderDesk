"""Isolated real scheduler/Blender test. Never operates on user processes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import psutil
from renderdesk.runtime import Runtime
from renderdesk.engine.protocol import read, digest
from renderdesk.processes import installations
from renderdesk.projects import scan_project
from renderdesk.adoption import infer_script


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--blender',default=(installations() or ['blender'])[0]);parser.add_argument('--upgrade',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='renderdesk-batch-'))
    blend=root/'scene.blend';setup=root/'setup.py';render=root/'render.py';plan=root/'plan.py'
    setup.write_text("import bpy,sys\ns=bpy.context.scene;s.frame_start=1;s.frame_end=12;s.render.engine='CYCLES';s.cycles.samples=2;s.render.resolution_x=48;s.render.resolution_y=32;s.render.resolution_percentage=100\nbpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])",encoding='utf8')
    subprocess.run([args.blender,'-b','--factory-startup','--python',str(setup),'--',str(blend)],check=True,stdout=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    render.write_text('''import bpy,sys,time
from pathlib import Path
argv=sys.argv[sys.argv.index('--')+1:];start=int(argv[0]);end=int(argv[1])
s=bpy.context.scene;s.render.image_settings.file_format='PNG'
out=Path(__file__).parent/'frames';out.mkdir(exist_ok=True)
for f in range(start,end+1):
 path=out/f'{f:04d}.png'
 if path.exists():continue
 s.frame_set(f);s.render.filepath=str(path);bpy.ops.render.render(write_still=True)
 print('BATCH_FRAME',f,flush=True)
 time.sleep(1)
''',encoding='utf8')
    plan.write_text('''from pathlib import Path
import subprocess,time
R=Path(__file__).resolve().parent
blender=BLENDER
for a in range(1,13,4):
 b=min(a+3,12)
 if all((R/'frames'/f'{i:04d}.png').exists() for i in range(a,b+1)):continue
 with (R/f'part_{a}.log').open('w') as log:
  r=subprocess.Popen([blender,'-b',str(R/'scene.blend'),'-t','1','--python',str(R/'render.py'),'--',str(a),str(b)],stdout=log,stderr=subprocess.STDOUT)
  while r.poll() is None:time.sleep(.1)
 if r.returncode!=0:raise SystemExit(1)
time.sleep(4)
(R/'postprocessing.done').write_text('done')
'''.replace('BLENDER',repr(args.blender)),encoding='utf8')
    runtime=Runtime(root/'data');parent=None;jid=None
    def task():return next(t for t in runtime.snapshot()['tasks'] if t['job']['id']==jid)
    def wait(predicate,timeout=90):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            current=task()
            if predicate(current):return current
            time.sleep(.1)
        raise AssertionError(runtime.snapshot())
    try:
        parent=subprocess.Popen([sys.executable,str(plan)],cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        until=time.monotonic()+30;child=None
        while time.monotonic()<until:
            children=[p for p in psutil.Process(parent.pid).children(recursive=True) if p.name().lower()=='blender.exe']
            if children and (root/'frames').exists():child=children[0];break
            time.sleep(.1)
        assert child is not None
        previous_id=None
        if args.upgrade:
            values=infer_script(render,child.cmdline(),root,scan_project(args.blender,blend))
            values['process']={'pid':child.pid,'created':child.create_time()}
            jid=runtime.submit('attach',values).result(20)
            previous_id=jid
            wait(lambda t:t['status']['state']=='complete' and not t['status']['running'])
            children=[p for p in psutil.Process(parent.pid).children(recursive=True) if p.name().lower()=='blender.exe']
            child=children[0]
        jid=runtime.submit('process.adopt',{'process':{'pid':child.pid,'created':child.create_time()}}).result(120)
        if previous_id:assert jid==previous_id
        first=wait(lambda t:t['status']['running'])
        assert first['status']['total']==12 and len(first['job']['batch']['batches'])==3
        initial=first['status']['pid']
        second=wait(lambda t:(t['status'].get('batch_range') or {}).get('start')==(9 if args.upgrade else 5))
        assert second['status']['pid']!=initial and second['status']['state']!='complete'
        runtime.submit('pause',{'id':jid}).result(10)
        paused=wait(lambda t:t['status']['state']=='paused' and not t['status']['running'])
        assert 4<=paused['status']['done']<12
        parent.wait(10)
        saved={p:digest(p) for p in (root/'frames').glob('*.png')}
        time.sleep(2)
        assert len(list((root/'frames').glob('*.png')))==len(saved)
        runtime.close();runtime=Runtime(root/'data')
        assert task()['status']['state']=='paused'
        runtime.submit('start',{'id':jid}).result(10)
        wait(lambda t:t['status']['state']=='postprocessing')
        assert not (root/'postprocessing.done').exists()
        complete=wait(lambda t:t['status']['state']=='complete' and not t['status']['running'])
        assert complete['status']['done']==12 and (root/'postprocessing.done').exists()
        assert read(root/'data/jobs'/jid/'batch-result.json')['exit_code']==0
        assert all(digest(p)==sha for p,sha in saved.items())
        assert 'BATCH_FRAME' in (root/'data/jobs'/jid/'render.log').read_text(encoding='utf8')
        runtime.submit('queue.reset',{'id':jid,'mode':'keep'}).result(10)
        time.sleep(2)
        assert task()['status']['state']=='ready'
        print('PASS: full plan, child PID rollover, parent/child pause, no next batch, restart recovery, resume skips saved images, postprocessing, logs, reset',flush=True)
        print(root,flush=True)
    finally:
        owned=[]
        if parent and parent.poll() is None:owned.append(psutil.Process(parent.pid))
        if jid:
            p=runtime.controller.batch_parent(jid)
            if p:owned.append(p)
        for p in owned:
            children=p.children(recursive=True)
            p.kill()
            for c in children:
                try:c.kill()
                except psutil.Error:pass
        runtime.close()


if __name__=='__main__':main()
