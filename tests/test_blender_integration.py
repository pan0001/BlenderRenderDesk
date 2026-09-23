import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import psutil

import argparse
import tempfile
from renderdesk.processes import installations
parser = argparse.ArgumentParser()
parser.add_argument('--blender', default=(installations() or ['blender'])[0])
options = parser.parse_args()
from renderdesk.engine.protocol import read, png_complete
from renderdesk.service import Controller

root = Path(tempfile.mkdtemp(prefix='renderdesk-integration-'))
blender = options.blender
blend = root / '测试 场景.blend'
setup = root / 'make_fixture.py'
setup.write_text('import bpy,sys\ns=bpy.context.scene\ns.render.engine=\"CYCLES\"\ns.cycles.samples=96\ns.render.resolution_x=256\ns.render.resolution_y=256\ns.render.resolution_percentage=100\ns.render.threads_mode=\"FIXED\"\ns.render.threads=1\nbpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])\n', encoding='utf8')
subprocess.run([blender, '--background', '--factory-startup', '--python', str(setup), '--', str(blend)], check=True, stdout=subprocess.DEVNULL, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
print('Integration workspace:',root,flush=True)
controller = Controller(root / 'data')
spawned = []


def wait(jid, check, label, timeout=100):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        controller.tick()
        state = controller.status(jid)
        if check(state):
            return state
        if state['state'] == 'error':
            raise AssertionError(label + '\n' + (controller.directory(jid) / 'render.log').read_text(encoding='utf8', errors='replace'))
        time.sleep(.08)
    raise AssertionError(label + ' timed out: ' + str(controller.status(jid)))


try:
    jid = controller.add({'blend': str(blend), 'blender': blender, 'start': 1, 'end': 4, 'step': 1, 'threads': 1, 'output': str(root / 'renders')})
    controller.schedule(jid, time.time() + .05)
    time.sleep(.08)
    controller.tick()
    wait(jid, lambda s: s['state'] == 'rendering', 'managed start')
    pid = read(controller.directory(jid) / 'launch.json')['pid']
    spawned += [pid]
    controller.close()
    controller = Controller(root / 'data')
    assert controller.live(jid)
    controller.pause(jid)
    state = wait(jid, lambda s: not s['running'], 'pause after manager restart')
    assert state['state'] == 'paused' and 0 < state['done'] < 4, state
    job = controller.jobs[jid]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(job['output']).glob('*.png')}
    elapsed = state['elapsed']
    print('PASS real Blender: scheduled start, reopen manager, frame-boundary pause, process exit', flush=True)
    controller.start(jid)
    new_pid = read(controller.directory(jid) / 'launch.json')['pid']
    spawned += [new_pid]
    assert new_pid != pid
    state = wait(jid, lambda s: not s['running'], 'managed resume')
    assert state['state'] == 'complete' and state['done'] == 4 and state['elapsed'] > elapsed, state
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in before.items())
    missing = Path(job['output']) / 'frame_000004.png'
    missing.unlink()
    controller.start(jid)
    spawned += [read(controller.directory(jid) / 'launch.json')['pid']]
    state = wait(jid, lambda s: not s['running'], 'missing-frame repair')
    assert png_complete(missing) and state['done'] == 4
    print('PASS real Blender: new PID resume, preserved frame hashes, cumulative elapsed, missing-frame repair', flush=True)

    out = root / 'external-output'
    out.mkdir()
    script = root / 'external_fixture.py'
    script.write_text('''import bpy, time
from pathlib import Path
s=bpy.context.scene
s.render.image_settings.file_format='PNG'
out=Path(__file__).parent/'external-output'
for frame in range(1,7):
    p=out/f'{frame:04d}.png'
    if p.exists(): continue
    s.frame_set(frame)
    s.render.filepath=str(p)
    bpy.ops.render.render(write_still=True)
    print('EXTERNAL_FIXTURE_FRAME', frame, flush=True)
    time.sleep(.4)
''', encoding='utf8')
    with (root / 'original.log').open('wb') as log:
        child = subprocess.Popen([blender, '-b', str(blend), '-t', '1', '--python', str(script)], stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    spawned += [child.pid]
    time.sleep(.3)
    rows, _ = controller.sampler.scan()
    row = next(r for r in rows if r['pid'] == child.pid)
    eid = controller.attach({'process': row, 'output': str(out), 'start': 1, 'end': 6, 'step': 1, 'pattern': '####.png'})
    controller.pause(eid)
    state = wait(eid, lambda s: not s['running'], 'external first pause')
    controller.tick()
    assert state['state'] == 'paused' and 0 < state['done'] < 6, state
    saved = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.png') if png_complete(p)}
    controller.start(eid)
    spawned += [read(controller.directory(eid) / 'launch.json')['pid']]
    wait(eid, lambda s: s['state'] == 'rendering', 'external cooperative start')
    controller.pause(eid)
    state = wait(eid, lambda s: not s['running'], 'external cooperative pause')
    assert state['state'] == 'paused' and 1 < state['done'] < 6, state
    controller.start(eid)
    spawned += [read(controller.directory(eid) / 'launch.json')['pid']]
    state = wait(eid, lambda s: not s['running'], 'external complete')
    assert state['state'] == 'complete' and state['done'] == 6, state
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == sha for p, sha in saved.items())
    assert 'EXTERNAL_FIXTURE_FRAME' in (controller.directory(eid) / 'render.log').read_text(encoding='utf8')
    print('PASS real Blender: external attach, PNG boundary stop, cooperative restart, preserved six frames, captured stdout', flush=True)
    # Queue operations requested during a frame must finish it before resetting/removing.
    qid=controller.add({'blend':str(blend),'blender':blender,'start':1,'end':4,'step':1,'threads':1,'output':str(root/'queue-output')})
    queue_output=Path(controller.jobs[qid]['output'])
    controller.start(qid)
    spawned.append(read(controller.directory(qid)/'launch.json')['pid'])
    wait(qid,lambda s:s['state']=='rendering','queue render start')
    assert controller.request_change(qid,'reset')['pending']
    state=wait(qid,lambda s:s['state']=='ready' and not s['running'],'live reset')
    assert state['done']==0 and state['elapsed']==0
    assert list((queue_output/'.renderdesk-history').rglob('*.png'))
    controller.start(qid)
    spawned.append(read(controller.directory(qid)/'launch.json')['pid'])
    wait(qid,lambda s:s['state']=='rendering','queue second start')
    assert controller.request_change(qid,'remove')['pending']
    deadline=time.monotonic()+100
    while qid in controller.jobs and time.monotonic()<deadline:
        controller.tick();time.sleep(.08)
    assert qid not in controller.jobs
    assert any(png_complete(p) for p in queue_output.glob('*.png'))
    print('PASS real Blender: live reset waits for saved frame, archives it, returns ready; live delete saves frame and removes only queue entry',flush=True)
    (root / 'result.json').write_text(json.dumps({'managed': controller.status(jid), 'external': state}, indent=2), encoding='utf8')
finally:
    # Only processes launched by this fixture are eligible for cleanup.
    for pid in spawned:
        try:
            p = psutil.Process(pid)
            if p.name().lower() == 'blender.exe' and str(root).lower() in ' '.join(p.cmdline()).lower():
                p.terminate()
                p.wait(10)
        except psutil.Error:
            pass
    controller.close()
