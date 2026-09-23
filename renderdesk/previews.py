"""Generate bounded previews only from a task's committed frame records."""
import hashlib
from pathlib import Path
import subprocess
import threading
from PIL import Image, ImageOps
from .engine.protocol import read, signature, frame_file, frames


class Previews:
    def __init__(self, root):
        self.root = Path(root)
        self.cache = self.root / 'previews'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def get(self, jid):
        if not jid.isalnum() or len(jid) > 64:
            raise ValueError('无效任务')
        directory = self.root / 'jobs' / jid
        job = read(directory / 'job.json')
        done = read(directory / 'progress.json', {'done': {}})['done']
        if not job or not done:
            raise ValueError('尚无完整帧可供预览')
        frame, record = max(done.items(), key=lambda pair: pair[1].get('mtime_ns', 0))
        path = Path(record['path']).resolve()
        if int(frame) not in frames(job) or path != frame_file(job, int(frame)).resolve():
            raise ValueError('帧记录与任务不匹配')
        if signature(path) != {k: record[k] for k in ('size', 'mtime_ns')}:
            raise ValueError('已完成图像已变化，暂不生成预览')
        key = hashlib.sha256((jid + str(path) + str(record['mtime_ns']) + str(record['size'])).encode()).hexdigest()
        target = self.cache / (jid + '-' + key[:20] + '.jpg')
        with self.lock:
            if target.exists():
                return target
            if path.suffix.lower() in ('.exr', '.hdr') or job.get('format') in ('OPEN_EXR', 'OPEN_EXR_MULTILAYER', 'HDR'):
                result = subprocess.run([job['blender'], '-b', '--factory-startup', '--disable-autoexec', '--python-exit-code', '23', '--python',
                    str(Path(__file__).parent / 'engine/thumbnail.py'), '--', str(path), str(target)],
                    capture_output=True, timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode or not target.exists():
                    raise ValueError('此图像暂时无法生成预览')
            else:
                with Image.open(path) as original:
                    original.draft('RGB', (960, 640))
                    original.thumbnail((960, 640))
                    preview = ImageOps.exif_transpose(original).convert('RGB')
                    preview.save(target, 'JPEG', quality=85)
            for old in self.cache.glob(jid + '-*.jpg'):
                if old != target:
                    old.unlink(missing_ok=True)
        return target
