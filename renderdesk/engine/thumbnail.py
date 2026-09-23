"""Read an HDR/EXR image without loading or executing a project."""
import sys
import bpy
args = sys.argv[sys.argv.index('--') + 1:]
image = bpy.data.images.load(args[0], check_existing=False)
w, h = image.size
scale = min(960 / max(1, w), 640 / max(1, h), 1)
image.scale(max(1, int(w * scale)), max(1, int(h * scale)))
scene = bpy.context.scene
scene.render.image_settings.file_format = 'JPEG'
scene.render.image_settings.color_mode = 'RGB'
scene.render.image_settings.quality = 85
image.save_render(args[1], scene=scene)
