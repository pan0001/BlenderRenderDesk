"""Read-only Blender project scan. Runs in Blender's own Python interpreter."""
import bpy
import json
import sys
from pathlib import Path

out = Path(sys.argv[sys.argv.index('--') + 1])
scenes = []
for s in bpy.data.scenes:
    r = s.render
    count = len(range(s.frame_start, s.frame_end + 1, s.frame_step))
    if count > 100000:
        raise ValueError('单场景最多支持扫描 100000 帧')
    paths = {str(f): str(Path(bpy.path.abspath(r.frame_path(frame=f))).resolve())
             for f in range(s.frame_start, s.frame_end + 1, s.frame_step)}
    scenes.append({'scene': s.name, 'camera': s.camera.name if s.camera else '',
        'start': s.frame_start, 'end': s.frame_end, 'step': s.frame_step, 'total': count,
        'format': r.image_settings.file_format, 'color_depth': r.image_settings.color_depth,
        'color_mode': r.image_settings.color_mode, 'engine': r.engine,
        'resolution_x': r.resolution_x, 'resolution_y': r.resolution_y,
        'resolution_percentage': r.resolution_percentage, 'fps': r.fps / r.fps_base,
        'filepath': r.filepath, 'use_file_extension': r.use_file_extension,
        'use_multiview': r.use_multiview, 'threads': r.threads if r.threads_mode == 'FIXED' else 0,
        'project_paths': paths, 'first_output': next(iter(paths.values()), ''),
        'preview_range': [s.frame_preview_start, s.frame_preview_end] if s.use_preview_range else None})
out.write_text(json.dumps({'active_scene': bpy.context.scene.name, 'scenes': scenes,
    'blender_version': bpy.app.version_string}, ensure_ascii=False), encoding='utf8')
