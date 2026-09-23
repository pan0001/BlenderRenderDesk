"""Blender-side cooperative renderer, independently implemented for Render Desk."""
import os
from pathlib import Path
import runpy
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import digest, frame_file, frames, png_complete, read, receipt, write
import bpy


class PauseAtBoundary(BaseException):
    pass


class RenderSession:
    def __init__(self, root):
        self.root = Path(root)
        self.job = read(self.root / 'job.json')
        self.done = read(self.root / 'progress.json', {'done': {}})['done']
        self.began = time.monotonic()
        self.accumulated = self.job.get('elapsed_base', 0)

    def status(self, state, **extra):
        data = {'state': state, 'updated': time.time(), 'elapsed_total': self.accumulated + time.monotonic() - self.began, **extra}
        write(self.root / 'status.json', data)
        print('[RenderDesk event]', state, extra, flush=True)

    def check_pause(self):
        control = read(self.root / 'control.json', {})
        if control.get('pause') or (control.get('pause_at') and time.time() >= control['pause_at']):
            raise PauseAtBoundary()

    def save_frame(self, scene, operation, args, kwargs):
        frame = scene.frame_current
        if frame not in frames(self.job):
            raise ValueError(f'渲染帧 {frame} 不在任务范围内')
        if str(frame) in self.done:
            return {'FINISHED'}
        self.check_pause()
        target = frame_file(self.job, frame)
        intent = read(self.root / 'intent.json', {})
        if target.exists():
            if intent.get('frame') == frame and intent.get('output') == str(target) and intent.get('sha256') == digest(target):
                self.done[str(frame)] = receipt(target)
                write(self.root / 'progress.json', {'done': self.done})
                return {'FINISHED'}
            raise ValueError(f'发现未确认的输出，已停止以免覆盖：{target}')
        if scene.render.use_multiview:
            raise ValueError('当前帧事务不支持多视图输出')
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_name('.renderdesk-' + self.job['id'] + '-' + target.name)
        original_path = scene.render.filepath
        started = time.monotonic()
        self.status('rendering', frame=frame, engine=scene.render.engine)
        try:
            scene.render.filepath = str(scratch)
            result = operation(*args, **kwargs)
            if 'FINISHED' not in result or not scratch.is_file() or not scratch.stat().st_size:
                raise RuntimeError(f'第 {frame} 帧未保存成功')
            if self.job['format'] == 'PNG' and not png_complete(scratch):
                raise RuntimeError(f'第 {frame} 帧 PNG 不完整')
            # Persist content identity before moving; recover a crash between move and receipt.
            write(self.root / 'intent.json', {'frame': frame, 'output': str(target), 'sha256': digest(scratch)})
            if target.exists():
                raise FileExistsError(str(target))
            # Windows rename refuses replacement; on other platforms use hard-link exclusive creation.
            if os.name == 'nt':
                os.rename(scratch, target)
            else:
                os.link(scratch, target)
                scratch.unlink()
            self.done[str(frame)] = receipt(target, round(time.monotonic() - started, 3))
            write(self.root / 'progress.json', {'done': self.done})
            self.status('rendering', frame=frame, saved=frame)
            print('[RenderDesk] FRAME_SAVED', frame, target, flush=True)
        finally:
            scene.render.filepath = original_path
        self.check_pause()
        return result

    def managed(self):
        scene = bpy.data.scenes[self.job['scene']] if self.job.get('scene') else bpy.context.scene
        if self.job.get('camera'):
            scene.camera = scene.objects[self.job['camera']]
        if not self.job.get('preserve_project'):
            scene.render.image_settings.file_format = self.job['format']
            if self.job['format'] == 'PNG' and scene.render.image_settings.color_depth == '32':
                scene.render.image_settings.color_depth = '16'
            scene.render.use_file_extension = True
            scene.render.use_placeholder = False
            scene.render.use_overwrite = True
        for frame in frames(self.job):
            if str(frame) in self.done:
                continue
            self.check_pause()
            scene.frame_set(frame)
            self.save_frame(scene, bpy.ops.render.render, (), {'write_still': True, 'scene': scene.name})

    def external(self):
        extension = self.job['external']
        operator_module = bpy.ops.render
        original = operator_module.render

        def intercept(*args, **kwargs):
            self.check_pause()
            if kwargs.get('animation') or not kwargs.get('write_still') or any(str(a).startswith('INVOKE') for a in args):
                raise ValueError('外部脚本续渲染需要同步逐帧 write_still 渲染')
            scene = bpy.data.scenes[kwargs['scene']] if kwargs.get('scene') else bpy.context.scene
            if scene.render.image_settings.file_format != 'PNG':
                raise ValueError('外部脚本接入仅支持 PNG 序列')
            actual = Path(bpy.path.abspath(scene.render.filepath))
            if actual.suffix.lower() != '.png':
                actual = Path(str(actual) + '.png')
            if actual.resolve() != frame_file(self.job, scene.frame_current).resolve():
                raise ValueError('脚本输出位置与接入配置不一致')
            return self.save_frame(scene, original, args, kwargs)

        operator_module.render = intercept
        bpy.ops.render = operator_module
        sys.path.insert(0, str(Path(extension['script']).parent))
        if '--python' in sys.argv:
            sys.argv[sys.argv.index('--python') + 1] = extension['script']
        try:
            runpy.run_path(extension['script'], run_name='__main__')
        finally:
            operator_module.render = original

    def run(self):
        try:
            self.status('loading')
            self.check_pause()
            self.external() if self.job.get('external') else self.managed()
            if len(self.done) != len(frames(self.job)):
                raise RuntimeError('脚本已结束，但仍有帧未完成')
            self.status('complete')
        except PauseAtBoundary:
            self.status('complete' if len(self.done) == len(frames(self.job)) else 'paused')
        except BaseException as error:
            traceback.print_exc()
            self.status('error', message=str(error))
            raise


if __name__ == '__main__':
    root = os.environ.get('RENDERDESK_JOB_DIR') or sys.argv[sys.argv.index('--') + 1]
    RenderSession(root).run()
