"""Executed by Blender, not by the desktop Python runtime."""
import json
import os
import sys
import time
import traceback
from pathlib import Path
import bpy

root = Path(sys.argv[sys.argv.index('--') + 1])

def read(name, default=None):
    p = root / name
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default

def write(name, value):
    p = root / name
    tmp = p.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05)

def state(s, **kw):
    write('status.json', {'state': s, 'updated': time.time(), **kw})
    print('[Render Desk]', s, json.dumps(kw, ensure_ascii=False), flush=True)

def pause_requested():
    return bool(read('control.json', {}).get('pause'))

def run():
    job = read('job.json')
    scene = bpy.context.scene
    if scene.render.use_multiview:
        raise RuntimeError('当前版本不支持多视图输出；请关闭 Stereoscopy / Multi-View 后新建任务。')
    scene.render.image_settings.file_format = job['format']
    scene.render.use_file_extension = True
    scene.render.use_overwrite = True
    scene.render.use_placeholder = False
    ext = '.png' if job['format'] == 'PNG' else '.exr'
    if job['format'] == 'PNG' and scene.render.image_settings.color_depth == '32':
        scene.render.image_settings.color_depth = '16'
    progress = read('progress.json', {'done': {}})
    frames = range(job['start'], job['end'] + 1, job['step'])
    state('loading', engine=scene.render.engine)
    node_tree = getattr(scene, 'node_tree', None) or getattr(scene, 'compositing_node_group', None)
    if node_tree:
        file_nodes = [n.name for n in node_tree.nodes if n.type == 'OUTPUT_FILE']
        if file_nodes:
            print('[Render Desk] 合成器 File Output 节点保留工程原路径：', file_nodes, flush=True)
    for frame in frames:
        if str(frame) in progress['done']:
            continue
        if pause_requested():
            state('paused')
            return
        output = Path(job['output']) / f'frame_{frame:06d}{ext}'
        # Intent is persisted before rendering. If Blender dies after commit but before receipt,
        # a full final file with this intent can safely be recognized on recovery.
        intent = read('intent.json', {})
        if output.exists():
            if intent.get('frame') == frame and intent.get('output') == str(output) and output.stat().st_size > 0:
                progress['done'][str(frame)] = {'path': str(output), 'size': output.stat().st_size}
                write('progress.json', progress)
                print('[Render Desk] 恢复已提交的帧：', frame, flush=True)
                continue
            raise RuntimeError(f'输出已存在且不属于已确认进度，停止以免覆盖：{output}')
        temporary = output.with_name(f'.rendering_{frame:06d}{ext}')
        write('intent.json', {'frame': frame, 'output': str(output)})
        scene.frame_set(frame)
        scene.render.filepath = str(temporary)
        state('rendering', frame=frame, engine=scene.render.engine)
        started = time.time()
        result = bpy.ops.render.render(write_still=True)
        if 'FINISHED' not in result or not temporary.exists() or temporary.stat().st_size == 0:
            raise RuntimeError(f'第 {frame} 帧未正确保存。')
        # rename (not replace) intentionally refuses to overwrite an existing final frame.
        os.rename(temporary, output)
        progress['done'][str(frame)] = {'path': str(output), 'size': output.stat().st_size,
                                       'seconds': round(time.time() - started, 3)}
        write('progress.json', progress)
        print(f'[Render Desk] FRAME_SAVED {frame} {output}', flush=True)
    state('complete')

try:
    run()
except Exception as e:
    traceback.print_exc()
    state('error', message=str(e))
    raise
