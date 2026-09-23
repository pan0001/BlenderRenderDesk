"""Queue changes, recoverable removal and frame-safe reset transactions."""
import copy
from pathlib import Path
import time
import uuid
from .engine.protocol import read, write, frames, frame_file, signature


class QueueActions:
    def ordered_ids(self):
        saved = self.catalogue.setting('queue_order', [])
        return [i for i in saved if i in self.jobs] + [i for i in self.jobs if i not in saved]

    def reorder(self, ids):
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)) or set(ids) != set(self.jobs):
            raise ValueError('队列已变化，请刷新后重新排序')
        self.catalogue.set_setting('queue_order', ids)
        return ids

    def pending(self, jid):
        return read(self.directory(jid) / 'queue-change.json', {})

    def ensure_editable(self, jid):
        if self.pending(jid):
            raise ValueError('任务正在等待队列操作完成')
        if self.live(jid):
            raise ValueError('请先按帧暂停，再编辑任务配置')

    def edit(self, jid, values):
        self.ensure_editable(jid)
        job = self.jobs[jid]
        updated = copy.deepcopy(job)
        allowed = {'name', 'start', 'end', 'step', 'threads', 'output', 'autoexec'}
        if set(values) - allowed:
            raise ValueError('存在不支持编辑的字段')
        updated.update(values)
        updated['name'] = str(updated['name']).strip()
        if not updated['name'] or len(updated['name']) > 160:
            raise ValueError('任务名称需为 1–160 个字符')
        updated = self.validate(updated)
        updated['output'] = str(Path(updated['output']).resolve())
        config_changed = any(updated.get(k) != job.get(k) for k in allowed - {'name'})
        if config_changed and read(self.directory(jid) / 'progress.json', {'done': {}})['done']:
            raise ValueError('已有完成帧，请先选择“从头重渲染”重置，再修改渲染配置；名称可直接修改')
        if job.get('external') and (updated['output'] != job['output'] or updated.get('threads') != job.get('threads')):
            raise ValueError('接管任务的输出路径和线程由原脚本控制，请保留这两项配置')
        if job.get('project_paths'):
            if any(str(f) not in job['project_paths'] for f in frames(updated)):
                raise ValueError('新范围超出已扫描范围，请先重新扫描工程')
            if updated['output'] != job['output']:
                updated['project_paths'] = {f: str(Path(updated['output']) / Path(path).name) for f, path in job['project_paths'].items()}
                updated['output_override'] = True
        if any(updated[k] != job[k] for k in ('start', 'end', 'step')):
            updated['range_source'] = 'override'
        self.save(updated)
        write(self.directory(jid) / 'job.json', updated)
        self.event(jid, '队列任务配置已更新')
        return jid

    def request_change(self, jid, action, mode='rerender'):
        if action not in ('reset', 'remove') or mode not in ('rerender', 'keep'):
            raise ValueError('无效的队列操作')
        existing = self.pending(jid)
        if existing:
            if existing['action'] != action or existing.get('mode') != mode:
                raise ValueError('已有待执行操作，请等待完成')
            existing.pop('error', None)
            write(self.directory(jid) / 'queue-change.json', existing)
        else:
            write(self.directory(jid) / 'queue-change.json', {'action': action, 'mode': mode, 'id': uuid.uuid4().hex, 'requested': time.time()})
        job = self.jobs[jid]
        job.update(start_at=None, pause_at=None)
        self.save(job)
        if self.live(jid):
            self.pause(jid)
            return {'pending': True}
        try:
            return self.apply_change(jid)
        except Exception as error:
            pending = self.pending(jid)
            pending['error'] = str(error)
            write(self.directory(jid) / 'queue-change.json', pending)
            raise

    def remove(self, jid, reason='manual'):
        if self.live(jid):
            raise ValueError('运行进程尚未退出')
        self.event(jid, '已从队列移除；工程、图片和历史日志保留。原因：' + reason)
        self.catalogue.remove(jid, reason)
        self.jobs.pop(jid)
        self.frame_cache.pop(jid, None)
        self.children.pop(jid, None)
        return {'removed': jid}

    def apply_change(self, jid):
        if self.live(jid):
            return {'pending': True}
        directory = self.directory(jid)
        pending = self.pending(jid)
        if not pending:
            return {'pending': False}
        if pending['action'] == 'remove':
            result = self.remove(jid)
            (directory / 'queue-change.json').unlink(missing_ok=True)
            return result
        job = self.jobs[jid]
        # The external process may have exited after writing its final PNG, before
        # the regular observer got another tick. Include that frame in the reset.
        if job.get('external', {}).get('phase') == 'attached' and 'plan' not in pending:
            self.observe_external(job)
        for other in self.jobs.values():
            if other['id'] != jid and self.live(other['id']) and Path(other['output']).resolve() == Path(job['output']).resolve():
                raise ValueError('同一输出目录还有其他任务运行，请等待其退出后重试重置')
        # Persist the complete move plan before archiving any output. The plan survives a crash.
        if 'plan' not in pending:
            records = read(directory / 'progress.json', {'done': {}})['done']
            plan = []
            if pending['mode'] == 'rerender':
                for f, record in records.items():
                    original = Path(record['path'])
                    expected = frame_file(job, int(f)).resolve()
                    if int(f) not in frames(job) or original.resolve() != expected or original.is_symlink():
                        raise ValueError('完成帧与任务不匹配，未移动任何文件')
                    if not original.exists():
                        continue
                    if signature(original) != {k: record[k] for k in ('size', 'mtime_ns')}:
                        raise ValueError('已完成图片发生变化，请先核对文件：' + str(original))
                    archive = expected.parent / '.renderdesk-history' / jid / pending['id'] / expected.name
                    archive.resolve().relative_to(expected.parent)
                    plan.append({'source': str(expected), 'target': str(archive), 'signature': signature(original)})
            pending.update(plan=plan, records=records, job=copy.deepcopy(job))
            write(directory / 'queue-change.json', pending)
        expected = {frame_file(job, int(f)).resolve() for f in pending['records']}
        for entry in pending['plan']:
            source, target = Path(entry['source']), Path(entry['target'])
            # Validate both paths again when resuming a reset journal.
            if source.resolve() not in expected:
                raise ValueError('重置归档路径无效')
            target.resolve().relative_to(source.parent.resolve() / '.renderdesk-history' / jid / pending['id'])
            if target.exists():
                if source.exists() or signature(target) != entry['signature']:
                    raise ValueError('重置归档文件冲突，请核对：' + str(target))
                continue
            if not source.exists() or source.is_symlink() or signature(source) != entry['signature']:
                raise ValueError('重置期间图像发生变化，请核对：' + str(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic same-volume move; target is unique and must never replace an existing file.
            source.rename(target)
        history = directory / 'history' / pending['id']
        write(history / 'reset.json', pending)
        updated = copy.deepcopy(job)
        updated.update(start_at=None, pause_at=None, elapsed_base=0, last_reset=pending['id'])
        if updated.get('external'):
            updated['external']['phase'] = 'managed'
        self.save(updated)
        write(directory / 'job.json', updated)
        done = {} if pending['mode'] == 'rerender' else pending['records']
        write(directory / 'progress.json', {'done': done})
        write(directory / 'control.json', {'pause': False, 'pause_at': None})
        write(directory / 'status.json', {'state': 'ready', 'elapsed_total': 0, 'updated': time.time()})
        for name in ('launch.json', 'intent.json', 'external_stop.json'):
            (directory / name).unlink(missing_ok=True)
        self.frame_cache.pop(jid, None)
        (directory / 'queue-change.json').unlink()
        self.event(jid, '状态已重置为等待；' + ('旧完成帧已归档，下次从头渲染。' if pending['mode'] == 'rerender' else '保留完成帧，只继续未完成部分。'))
        return {'pending': False, 'reset': jid, 'archived': len(pending['plan']), 'mode': pending['mode']}

    def process_changes(self):
        for jid in list(self.jobs):
            pending = self.pending(jid)
            if not pending or pending.get('error') or self.live(jid):
                continue
            try:
                self.apply_change(jid)
            except Exception as error:
                pending = self.pending(jid)
                pending['error'] = str(error)
                write(self.directory(jid) / 'queue-change.json', pending)
                self.notices.append('队列操作未完成：' + str(error))

    def cleanup_completed(self):
        removed = 0
        for jid in list(self.jobs):
            try:
                state = self.status(jid)
                if not state['running'] and state['state'] == 'complete' and state['done'] == state['total'] and not self.pending(jid):
                    self.remove(jid, 'completed_on_startup')
                    removed += 1
            except Exception as error:
                self.notices.append('启动清理跳过异常任务：' + str(error))
        if removed:
            self.notices.append(f'启动时已清理 {removed} 个完成任务；工程、渲染图片和历史日志均保留。')
        return removed
