"""Real Blender one-command adoption, original PID preservation, pause and resume."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
import psutil
from renderdesk.runtime import Runtime
from renderdesk.engine.protocol import digest, read
from renderdesk.processes import installations


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--blender',default=(installations() or ['blender'])[0]);args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='renderdesk-auto-adopt-'))
    blend=root/'project.blend';setup=root/'setup.py';script=root/'render.py'
    setup.write_text("import bpy,sys\ns=bpy.context.scene\ns.frame_start=1;s.frame_end=1200\ns.render.engine='CYCLES';s.cycles.samples=4\ns.render.resolution_x=48;s.render.resolution_y=32;s.render.resolution_percentage=100\ns.render.filepath='//wrong-saved-path/'\nbpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])\n",encoding='utf8')
    subprocess.run([args.blender,'-b','--factory-startup','--python',str(setup),'--',str(blend)],check=True,stdout=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    script.write_text('''import bpy,sys,time
from pathlib import Path
args=sys.argv[sys.argv.index('--')+1:]
mode=args[0];start=int(args[1]);end=int(args[2])
s=bpy.context.scene;s.render.image_settings.file_format='PNG'
out=Path(__file__).parent/'actual-images'/mode
out.mkdir(parents=True,exist_ok=True)
for f in range(start,end+1):
    path=out/f'{f:04d}.png'
    if path.exists():continue
    s.frame_set(f);s.render.filepath=str(path)
    bpy.ops.render.render(write_still=True)
    print('AFTER_WRITE',f,flush=True)
    time.sleep(1)
''',encoding='utf8')
    runtime=Runtime(root/'data');child=None
    def wait(jid,predicate,timeout=60):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            task=next(t for t in runtime.snapshot()['tasks'] if t['job']['id']==jid)
            if predicate(task):return task
            if task['status']['state']=='error':raise AssertionError(task)
            time.sleep(.1)
        raise AssertionError(runtime.snapshot())
    try:
        with (root/'original.log').open('wb') as log:
            child=subprocess.Popen([args.blender,'-b',str(blend),'-t','1','--python',str(script),'--','final','181','190'],stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        row={'pid':child.pid,'created':psutil.Process(child.pid).create_time()}
        jid=runtime.submit('process.adopt',{'process':row}).result(180)
        task=wait(jid,lambda t:t['status']['running'])
        assert (task['job']['start'],task['job']['end'],task['status']['total'])==(181,190,10)
        assert task['job']['project']['total']==1200
        assert Path(task['job']['output'])==root/'actual-images/final'
        assert task['job']['external']['pattern']=='####.png'
        assert task['status']['pid']==child.pid and child.poll() is None
        runtime.submit('project.rescan',{'id':jid}).result(180)
        assert next(t for t in runtime.snapshot()['tasks'] if t['job']['id']==jid)['status']['total']==10
        runtime.submit('pause',{'id':jid}).result(10)
        stopped=wait(jid,lambda t:not t['status']['running'])
        assert 0<stopped['status']['done']<10,stopped
        saved={p:digest(p) for p in (root/'actual-images/final').glob('*.png')}
        runtime.submit('start',{'id':jid}).result(10)
        complete=wait(jid,lambda t:t['status']['state']=='complete' and not t['status']['running'])
        assert complete['status']['done']==10
        assert all(digest(p)==sha for p,sha in saved.items())
        assert 'AFTER_WRITE' in (root/'data/jobs'/jid/'render.log').read_text(encoding='utf8')
        print('PASS one-command adoption: actual output, CLI subset 181–190 / project 1200, original PID continues, rescan preserves subset, frame-boundary pause, resumed 10 frames, logs and saved image hashes',flush=True)
        print('Workspace:',root,flush=True)
    finally:
        for p in [child,*runtime.controller.children.values()]:
            if p and p.poll() is None:p.terminate();p.wait(10)
        runtime.close()


if __name__=='__main__':main()
