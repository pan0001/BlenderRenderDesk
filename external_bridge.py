"""Restart a user-selected render script with cooperative per-frame control."""
import os
import sys
import json
import runpy
import time
import traceback
from pathlib import Path
import bpy

root=Path(os.environ['RENDERDESK_JOB_DIR'])
sys.path.insert(0,str(root))
from external_utils import valid_png, frame_path
job=json.loads((root/'job.json').read_text(encoding='utf-8'))

def read(name):
    return json.loads((root/name).read_text(encoding='utf-8'))
def write(name,value):
    p=root/name; tmp=p.with_suffix('.bridge.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    for retry in range(20):
        try: os.replace(tmp,p);return
        except PermissionError:
            if retry==19: raise
            time.sleep(.05)
def state(value,**kw):
    write('status.json',{'state':value,'updated':time.time(),**kw})
    print('[Render Desk]',value,kw,flush=True)
class PauseRender(BaseException): pass

render_module=bpy.ops.render
original=render_module.render
def controlled_render(*args,**kwargs):
    if read('control.json').get('pause'): raise PauseRender()
    if kwargs.get('animation') or any(isinstance(a,str) and a.startswith('INVOKE') for a in args):
        raise RuntimeError('接入模式只支持同步、逐帧渲染，不支持脚本内动画批渲染。')
    scene=bpy.data.scenes[kwargs['scene']] if kwargs.get('scene') else bpy.context.scene
    f=scene.frame_current
    if f not in range(job['start'],job['end']+1,job['step']):
        raise RuntimeError('原脚本渲染帧超出接入范围，请检查范围。')
    if scene.render.image_settings.file_format!='PNG' or not kwargs.get('write_still') or scene.render.use_multiview:
        raise RuntimeError('接入模式只支持 write_still=True 的单视图 PNG 序列。')
    path=Path(bpy.path.abspath(scene.render.filepath))
    if path.suffix.lower()!='.png': path=Path(str(path)+'.png')
    expected=frame_path(job,f)
    if path.resolve()!=expected.resolve(): raise RuntimeError(f'原脚本输出位置与接入配置不同：{path}')
    progress=read('progress.json')
    if str(f) in progress['done'] and valid_png(path): return {'FINISHED'}
    state('rendering',frame=f)
    result=original(*args,**kwargs)
    if 'FINISHED' not in result or not valid_png(path): raise RuntimeError(f'第 {f} 帧未完整保存。')
    st=path.stat()
    progress['done'][str(f)]={'path':str(path),'size':st.st_size,'mtime_ns':st.st_mtime_ns}
    write('progress.json',progress)
    print('[Render Desk] FRAME_SAVED',f,str(path),flush=True)
    if read('control.json').get('pause'): raise PauseRender()
    return result

render_module.render=controlled_render
bpy.ops.render=render_module
try:
    state('loading')
    sys.path.insert(0,str(Path(job['external']['script']).parent))
    if '--python' in sys.argv:
        sys.argv[sys.argv.index('--python')+1]=job['external']['script']
    runpy.run_path(job['external']['script'],run_name='__main__')
    count=len(range(job['start'],job['end']+1,job['step']))
    if len(read('progress.json')['done'])!=count: raise RuntimeError('脚本已退出，但接入范围内仍有未完成帧。')
    state('complete')
except PauseRender:
    state('paused')
except BaseException as e:
    traceback.print_exc();state('error',message=str(e))
finally:
    render_module.render=original
