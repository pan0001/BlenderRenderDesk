"""Attach to existing background PNG scripts without code injection.

The first stop is observational: a completed PNG is detected, then the existing
process is terminated. A subsequent partially started frame may be lost. Resumes
use a cooperative bridge and never require terminating the render process.
"""
import ast
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
import psutil
from external_utils import inspect_frames, frame_path, valid_png

def script_info(row):
    args = row['args']
    if not any(a in ('-b','--background') for a in args):
        raise ValueError('仅支持接入后台渲染进程，不会结束有编辑窗口的 Blender。')
    if args.count('--python') != 1 or any(a in args for a in ('--python-expr','-a','--render-anim','-f','--render-frame')):
        raise ValueError('当前支持单个 --python 脚本逐帧输出 PNG 的后台进程。其他启动方式暂不支持接入。')
    script = Path(args[args.index('--python')+1]).resolve()
    if not script.is_file() or not row.get('blend'):
        raise ValueError('无法读取渲染脚本或 .blend 路径。')
    tail = args[args.index('--')+1:] if '--' in args else []
    return script, tail

def suggest(row):
    script, tail = script_info(row)
    # Interpret only a few literal pathlib expressions; never execute script code.
    env = {'__file__':str(script), 'mode':tail[0] if tail else 'final'}
    def value(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str): return n.value
        if isinstance(n, ast.Name): return env[n.id]
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div): return Path(value(n.left)) / value(n.right)
        if isinstance(n, ast.Attribute) and n.attr == 'parent': return Path(value(n.value)).parent
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id == 'Path' and len(n.args)==1: return Path(value(n.args[0]))
            if isinstance(n.func, ast.Attribute) and n.func.attr == 'resolve' and not n.args: return Path(value(n.func.value)).resolve()
        raise ValueError('not a literal path')
    for n in ast.parse(script.read_text(encoding='utf-8-sig')).body:
        if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name):
            try: env[n.targets[0].id]=value(n.value)
            except (ValueError,KeyError,TypeError): pass
    output = env.get('out', script.parent)
    start, end, step = 1, 250, 1
    if len(tail)>=3:
        try: start,end=int(tail[1]),int(tail[2])
        except ValueError: pass
    return {'output':str(output),'start':start,'end':end,'step':step,'pattern':'####.png','script':str(script)}

def attach(manager, row, output, start, end, step, pattern):
    from core import alive, fingerprint, atomic_json, read_json
    script, _ = script_info(row)
    identity={'pid':row['pid'],'created':row['created']}
    if not alive(identity): raise ValueError('选中的进程已退出或 PID 已变化，请刷新列表。')
    if not re.fullmatch(r'[^/\\{}#]*#{1,10}[^/\\{}#]*\.png',pattern,re.IGNORECASE):
        raise ValueError('文件名应类似 ####.png 或 frame_######.png，只支持一个连续的 # 帧号段。')
    if not -1048574 <= start <= end <= 1048574 or step<1 or len(range(start,end+1,step))>100000:
        raise ValueError('帧范围无效，最多支持 100000 帧。')
    out=Path(output).resolve()
    if not out.is_dir(): raise ValueError('请选择此进程实际正在写入的 PNG 输出目录。')
    for j in manager.jobs.values():
        launch = read_json(manager.directory(j['id'])/'launch.json')
        if manager.live(j['id']) and (Path(j['output']).resolve()==out or launch.get('pid')==row['pid']):
            raise ValueError('此进程或输出目录已由一个任务管理。')
    p=psutil.Process(row['pid'])
    cwd=p.cwd()
    jid=uuid.uuid4().hex[:12]
    job={'id':jid,'name':Path(row['blend']).stem,'blend':row['blend'],'blender':row['exe'],
         'start':start,'end':end,'step':step,'output':str(out),'format':'PNG','threads':0,
         'autoexec':False,'source':fingerprint(row['blend']),'created_at':time.time(),
         'start_at':None,'pause_at':None,
         'external':{'phase':'attached','script':str(script),'script_source':fingerprint(script),
                     'args':row['args'],'cwd':cwd,'pattern':pattern}}
    manager.jobs[jid]=job
    manager.save(job)
    d=manager.directory(jid)
    atomic_json(d/'launch.json',identity)
    atomic_json(d/'control.json',{'pause':False})
    atomic_json(d/'status.json',{'state':'rendering','message':'已接入外部进程，观察 PNG 保存事件'})
    atomic_json(d/'progress.json',{'done':inspect_frames(job)})
    manager.log(jid,'已接入现有进程。首次暂停为检测新帧完整落盘后结束进程，可能丢弃刚开始的下一帧；续渲染后使用帧边界协作暂停。原进程的历史 stdout 无法补接。')
    return job

def refresh(manager, job):
    from core import atomic_json, read_json, alive
    jid=job['id']; d=manager.directory(jid)
    prior=read_json(d/'progress.json',{'done':{}})['done']
    cache=manager.external_cache.setdefault(jid,{})
    done=inspect_frames(job,cache)
    if done != prior:
        atomic_json(d/'progress.json',{'done':done})
        for f in sorted(set(done)-set(prior),key=int): manager.log(jid,f'EXTERNAL_FRAME_SAVED {f} {done[f]["path"]}')
    launch=read_json(d/'launch.json')
    total=len(range(job['start'],job['end']+1,job['step']))
    control=read_json(d/'control.json')
    stopped=read_json(d/'external_stop.json')
    if alive(launch) and control.get('pause') and not stopped:
        baseline=control.get('baseline',{})
        changed=[f for f, rec in done.items() if baseline.get(f) != rec]
        if changed:
            process=psutil.Process(launch['pid'])
            # Recheck identity on the actual process object immediately before acting.
            if abs(process.create_time()-launch['created'])>.01: raise ValueError('进程身份已改变，取消停止。')
            process.terminate()
            atomic_json(d/'external_stop.json',{'requested':time.time(),'last_saved':max(map(int,changed))})
            manager.log(jid,'已检测新帧完整落盘并请求结束旧进程；下次继续将使用协作式帧边界控制。')
    if not alive(launch):
        state='complete' if len(done)==total else ('paused' if read_json(d/'external_stop.json') else 'interrupted')
        atomic_json(d/'status.json',{'state':state,'updated':time.time()})
        job['external']['phase']='managed'
        manager.save(job)

def pause(manager, job):
    from core import atomic_json
    baseline=inspect_frames(job,manager.external_cache.setdefault(job['id'],{}))
    atomic_json(manager.directory(job['id'])/'control.json',{'pause':True,'baseline':baseline})
    manager.log(job['id'],'请求首次暂停：等待下一张完整 PNG 落盘，再结束选中的旧进程。')

def start(manager, job):
    from core import atomic_json, fingerprint, resource
    jid=job['id']; d=manager.directory(jid)
    ext=job['external']
    if fingerprint(ext['script']) != ext['script_source']:
        raise ValueError('原渲染脚本已修改，请创建新任务，避免混用渲染设置。')
    done=inspect_frames(job)
    if len(done)==len(range(job['start'],job['end']+1,job['step'])):
        raise ValueError('全部帧已完成。')
    # Preserve partial images instead of letting the original script skip them.
    for frame in range(job['start'],job['end']+1,job['step']):
        p=frame_path(job,frame)
        if p.exists() and str(frame) not in done:
            dest=p.with_name(p.name+'.incomplete-'+uuid.uuid4().hex[:8])
            p.rename(dest)
            manager.log(jid,'保留未完成文件：'+str(dest))
    atomic_json(d/'progress.json',{'done':done})
    atomic_json(d/'control.json',{'pause':False})
    atomic_json(d/'status.json',{'state':'starting'})
    for name in ('external_bridge.py','external_utils.py'):
        shutil.copyfile(resource(name),d/name)
    command=list(ext['args'])
    command[command.index('--python')+1]=str(d/'external_bridge.py')
    env=os.environ.copy()
    env.update(RENDERDESK_JOB_DIR=str(d),PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
    ext['phase']='managed';job['start_at']=None;manager.save(job)
    manager.log(jid,'使用原脚本与原参数续渲染；启动协作控制与完整 stdout 日志。')
    try:
        with (d/'render.log').open('ab',buffering=0) as log:
            child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                 cwd=ext['cwd'],env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        manager.children[jid]=child
        atomic_json(d/'launch.json',{'pid':child.pid,'created':psutil.Process(child.pid).create_time()})
    except Exception as e:
        atomic_json(d/'status.json',{'state':'error','message':str(e),'updated':time.time()})
        raise
