"""Original Render Desk disk protocol; usable inside Blender's Python runtime."""
from pathlib import Path
import hashlib
import json
import os
import re
import struct
import time
import uuid
import zlib


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with scratch.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(scratch, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.025)
    finally:
        scratch.unlink(missing_ok=True)


def signature(path):
    info = Path(path).stat()
    return {'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def frames(job):
    return range(int(job['start']), int(job['end']) + 1, int(job['step']))


def frame_file(job, frame):
    if job.get('project_paths') and not job.get('external'):
        return Path(job['project_paths'][str(frame)])
    pattern = job.get('external', {}).get('pattern')
    if pattern:
        match = re.search(r'#+', pattern)
        name = pattern[:match.start()] + str(frame).zfill(len(match[0])) + pattern[match.end():]
    else:
        name = f"frame_{frame:06d}." + ('png' if job['format'] == 'PNG' else 'exr')
    return Path(job['output']) / name


def png_complete(path):
    """Validate the entire PNG container, including chunk CRCs and final IEND."""
    try:
        with Path(path).open('rb') as stream:
            if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                return False
            first, pixels = True, False
            while True:
                header = stream.read(8)
                if len(header) != 8:
                    return False
                length, kind = struct.unpack('>I4s', header)
                if length > 536870912 or (first and (kind != b'IHDR' or length != 13)):
                    return False
                first = False
                crc = zlib.crc32(kind)
                left = length
                while left:
                    block = stream.read(min(left, 1048576))
                    if not block:
                        return False
                    left -= len(block)
                    crc = zlib.crc32(block, crc)
                stored = stream.read(4)
                if len(stored) != 4 or struct.unpack('>I', stored)[0] != crc & 0xffffffff:
                    return False
                pixels |= kind == b'IDAT'
                if kind == b'IEND':
                    return length == 0 and pixels and not stream.read(1)
    except OSError:
        return False


def receipt(path, seconds=0):
    return {'path': str(path), **signature(path), 'sha256': digest(path), 'seconds': seconds}


def verify(record):
    path = Path(record['path'])
    if not path.exists():
        return False
    if path.stat().st_size != record['size'] or not record['size']:
        raise ValueError(f'已完成文件被修改：{path}')
    if record.get('sha256') and digest(path) != record['sha256']:
        raise ValueError(f'已完成文件校验失败：{path}')
    return True
