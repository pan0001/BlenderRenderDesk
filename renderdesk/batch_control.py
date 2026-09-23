"""Manage a recognized sequential scheduler and its changing Blender children."""
from pathlib import Path
import os
import shutil
import subprocess
import time
import psutil
from .batch import identity, current_children, command_matches
from .processes import process
from .engine.protocol import read, write, signature, frames, frame_file


class BatchActions:
    def batch_parent(self, jid):
        directory=self.directory(jid)
        item=process(read(directory/'batch-launch.json', {}))
        if item:
            # Windows venv python.exe can be a launcher waiting on the actual interpreter.
            expected=str(directory/'batch_runner.py')
            for child in item.children():
                try:
                    args=child.cmdline()
                    if len(args)>1 and Path(args[1]).resolve()==Path(expected).resolve() and Path(child.exe()).stem.lower().startswith('python'):
                        write(directory/'batch-launch.json',identity(child))
                        return child
                except psutil.Error:
                    pass
        return item

    def batch_children(self, jid):
        directory=self.directory(jid)
        saved=read(directory/'batch-children.json', [])
        known={r['pid']:r for r in saved if process(r)}
        for item,batch in current_children(self.jobs[jid]['batch'], self.batch_parent(jid)):
            known[item.pid]={**identity(item),'start':batch['start'],'end':batch['end'],'log':batch.get('log')}
        rows=list(known.values())
        if rows!=saved: write(directory/'batch-children.json',rows)
        return rows

    def batch_live(self, jid):
        return bool(self.batch_parent(jid) or self.batch_children(jid))

    def attach_batch(self, values):
        plan=values['batch'];parent=process(plan['identity'])
        if not parent or signature(plan['script'])!=plan['source']:
            raise ValueError('调度脚本已退出或发生变化，请重新识别')
        matches=current_children(plan,parent)
        # The initially clicked child may have finished during a large project scan.
        if not matches:
            raise ValueError('当前处于批次切换间隙，请稍后重新接管')
        if len(matches)!=1:
            raise ValueError('发现并行渲染进程，当前仅支持顺序分批脚本')
        script=Path(matches[0][0].cwd())/matches[0][0].cmdline()[matches[0][0].cmdline().index('--python')+1]
        if signature(script)!=values['detected_script_source']:
            raise ValueError('识别后渲染脚本发生变化，请重新接管')
        values={**values,'process':identity(matches[0][0]),'start':plan['start'],'end':plan['end'],
                'step':plan['step'],'range_source':'batch'}
        for jid,job in self.jobs.items():
            if job.get('batch') and read(self.directory(jid)/'batch-launch.json',{})==plan['identity']:
                return jid
        reusable=None
        for jid,job in self.jobs.items():
            if Path(job['output']).resolve()!=Path(values['output']).resolve():continue
            launch=read(self.directory(jid)/'launch.json',{})
            if self.live(jid) and launch.get('pid')!=matches[0][0].pid:
                raise ValueError('该输出目录已有其他运行任务，不能重复接管')
            if (job.get('auto_detected') and not job.get('batch') and job['source']==values['source']
                and Path(job['blend']).resolve()==Path(values['blend']).resolve()
                and job.get('external',{}).get('script')==str(Path(matches[0][0].cwd(),matches[0][0].cmdline()[matches[0][0].cmdline().index('--python')+1]).resolve())
                and job['external']['pattern']==values['pattern'] and not self.pending(jid)):
                reusable=jid
        if reusable:
            jid=reusable;job=self.jobs[jid]
            job.update(start=values['start'],end=values['end'],step=values['step'],project=values['project'],
                       batch=plan,range_source='batch',start_at=None,pause_at=None,elapsed_base=0)
            job['external'].update(phase='attached',args=matches[0][0].cmdline())
            self.save(job)
        else:
            # Standard attachment validates the process and PNG output contract again.
            jid=self.attach(values);job=self.jobs[jid]
        directory=self.directory(jid)
        write(directory/'batch-launch.json',plan['identity'])
        write(directory/'launch.json',identity(matches[0][0]))
        write(directory/'batch-children.json',[])
        write(directory/'job.json',job)
        write(directory/'control.json',{'pause':False})
        (directory/'external_stop.json').unlink(missing_ok=True)
        write(directory/'progress.json',{'done':self.inspect_external(job)})
        write(directory/'status.json',{'state':'rendering','updated':time.time()})
        self.event(jid,f"已接管外层脚本 {Path(plan['script']).name}：{len(plan['batches'])} 批，计划 {plan['start']}–{plan['end']} 帧；后续 Blender 自动归入此任务。")
        return jid

    def pause_batch(self, jid):
        directory=self.directory(jid);job=self.jobs[jid]
        parent=self.batch_parent(jid)
        rows=self.batch_children(jid)
        if not parent and not rows:raise ValueError('脚本和渲染进程均已退出')
        done=self.inspect_external(job)
        if parent and len(done)==len(frames(job)):
            raise ValueError('全部图像已保存，外层脚本可能正在合成或校验，请等待脚本结束')
        control=read(directory/'control.json',{})
        if control.get('pause'):return
        # Persist the intent before suspending the scheduler; restart can finish it.
        write(directory/'control.json',{'pause':True,'baseline':done})
        try:
            if parent:
                parent.suspend()
        except psutil.Error:
            if process(identity(parent)):
                write(directory/'control.json',{'pause':False})
                raise
        job.update(start_at=None,pause_at=None);self.save(job)
        self.event(jid,'已暂停外层调度，阻止启动下一批；等待当前帧完整保存后退出脚本与 Blender。')
        self.observe_batch(job)

    def observe_batch(self, job):
        jid=job['id'];directory=self.directory(jid)
        if not (directory/'batch-launch.json').exists():return
        parent=self.batch_parent(jid);rows=self.batch_children(jid)
        old=read(directory/'status.json',{})
        if not parent and not rows and old.get('state') in ('paused','complete','interrupted'):
            return
        done=self.inspect_external(job)
        previous=read(directory/'progress.json',{'done':{}})['done']
        if done!=previous:
            write(directory/'progress.json',{'done':done})
            for f in done.keys()-previous.keys():self.event(jid,f'检测到完整 PNG：{f}')
        if rows:
            write(directory/'launch.json',{'pid':rows[0]['pid'],'created':rows[0]['created']})
            self.read_batch_log(jid,rows[0].get('log'))
        else:
            reader=getattr(self,'batch_log_readers',{}).get(jid)
            if reader:self.read_batch_log(jid,reader.path)
        control=read(directory/'control.json',{})
        stopping=control.get('pause')
        if stopping:
            # A recovered pause intent must also stop the scheduler before a child exits.
            if parent and parent.status()!=psutil.STATUS_STOPPED:
                parent.suspend()
            boundary=not rows or any(control.get('baseline',{}).get(f)!=r for f,r in done.items())
            if boundary:
                # Parent first: it cannot react to a child's exit by launching more work.
                selected=self.batch_parent(jid)
                if selected:selected.terminate()
                for row in rows:
                    selected=process(row)
                    if selected:selected.terminate()
                write(directory/'external_stop.json',{'requested':time.time()})
        running=self.batch_live(jid)
        if running:
            state='watching' if stopping else 'postprocessing' if len(done)==len(frames(job)) else 'rendering' if rows else 'batch_waiting'
        elif stopping:
            state='paused'
        else:
            result=read(directory/'batch-result.json',{})
            state='complete' if len(done)==len(frames(job)) and result.get('exit_code',0)==0 else 'interrupted'
        launch=read(directory/'batch-launch.json',{})
        elapsed=job.get('elapsed_base',0)+max(0,time.time()-launch.get('created',time.time())) if running or old.get('state') in ('rendering','watching','batch_waiting','postprocessing') else old.get('elapsed_total',job.get('elapsed_base',0))
        message=''
        if state=='postprocessing':message='计划内图像已保存，等待外层脚本合成、校验或退出。'
        if state=='complete' and not (directory/'batch-result.json').exists():message='计划帧已全部保存，原脚本已退出；原进程的退出码和后处理结果无法补取。'
        if state=='interrupted':message='调度脚本已退出，计划尚未确认完成；可查看日志后继续。'
        updated={'state':state,'updated':time.time(),'elapsed_total':elapsed,'message':message}
        write(directory/'status.json',updated)

    def read_batch_log(self, jid, path):
        if not path or not Path(path).is_file():return
        from .service import LogReader
        readers=getattr(self,'batch_log_readers',None)
        if readers is None:self.batch_log_readers={};readers=self.batch_log_readers
        reader=readers.setdefault(jid,LogReader())
        reset,text=reader.poll(path)
        if text:
            with (self.directory(jid)/'render.log').open('a',encoding='utf8') as stream:
                if reset:stream.write('\n[RenderDesk 批次日志] '+str(path)+'\n')
                stream.write(text)

    def batch_status(self, jid, state, indexed=None):
        rows=self.batch_children(jid);parent=self.batch_parent(jid)
        job=self.jobs[jid];saved=read(self.directory(jid)/'status.json',{})
        metrics=[(indexed or {}).get((r['pid'],r['created']),{}) for r in rows]
        result={**state,**saved,'running':bool(parent or rows),'pid':rows[0]['pid'] if rows else None,
                'scheduler_pid':parent.pid if parent else None,'managed_pids':[r['pid'] for r in rows],
                'batch_range':{'start':rows[0]['start'],'end':rows[0]['end']} if rows else None,
                'elapsed':saved.get('elapsed_total',0),'total':len(frames(job))}
        for key in ('cpu','ram_mb','gpu','vram_mb'):
            known=[r[key] for r in metrics if r.get(key) is not None]
            result[key]=sum(known) if known else None
        return result

    def start_batch(self, jid):
        job=self.jobs[jid];plan=job['batch'];directory=self.directory(jid)
        if self.pending(jid):raise ValueError('请先完成队列操作')
        if self.batch_live(jid):raise ValueError('外层脚本或其 Blender 仍在运行')
        for item in psutil.process_iter(['name']):
            if (item.info['name'] or '').lower() not in ('blender','blender.exe'):continue
            try:
                if any(command_matches(item.cmdline(),b['args']) for b in plan['batches']):
                    raise ValueError('该计划仍有未退出的 Blender，请先在本机进程中重新接管')
            except psutil.Error:
                continue
        for path,expected in [(job['blend'],job['source']),(plan['script'],plan['source']),
                              (job['external']['script'],job['external']['script_source'])]:
            if signature(path)!=expected:raise ValueError('工程或调度/渲染脚本已修改，请重新接管')
        for other in self.jobs.values():
            if other['id']!=jid and self.live(other['id']) and Path(other['output']).resolve()==Path(job['output']).resolve():
                raise ValueError('输出目录还有其他任务运行')
        valid=self.inspect_external(job)
        # Original scheduler skips existing frames. Archive incomplete PNGs so it cannot skip them.
        for f in frames(job):
            path=frame_file(job,f)
            if path.exists() and str(f) not in valid:
                import uuid
                path.rename(path.with_name(path.name+'.incomplete-'+uuid.uuid4().hex[:8]))
        job['elapsed_base']=read(directory/'status.json',{}).get('elapsed_total',0)
        job['start_at']=None
        self.save(job);write(directory/'job.json',job)
        for name in ('batch_runner.py','protocol.py'):
            shutil.copyfile(Path(__file__).parent/'engine'/name,directory/name)
        write(directory/'control.json',{'pause':False,'pause_at':job.get('pause_at')})
        write(directory/'batch-children.json',[])
        for name in ('external_stop.json','batch-result.json'):(directory/name).unlink(missing_ok=True)
        command=[plan['args'][0],str(directory/'batch_runner.py'),str(directory)]
        with (directory/'render.log').open('ab',buffering=0) as log:
            child=subprocess.Popen(command,cwd=plan['cwd'],stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,
                                   env={**os.environ,'PYTHONUNBUFFERED':'1','PYTHONIOENCODING':'utf-8'},
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.children[jid]=child
        write(directory/'batch-launch.json',identity(psutil.Process(child.pid)))
        write(directory/'status.json',{'state':'batch_waiting','updated':time.time(),'elapsed_total':job['elapsed_base']})
        self.event(jid,'已重新启动原调度脚本；沿用原分批流程，跳过完整输出，保留最后合成和校验步骤。')
