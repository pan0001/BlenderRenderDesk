"""PNG sequence validation shared by the manager and the Blender bridge."""
import re
import os
import struct
import zlib
from pathlib import Path

def frame_path(job, frame):
    pattern = job['external']['pattern']
    hashes = re.search(r'#+', pattern)
    name = pattern[:hashes.start()] + f'{frame:0{len(hashes.group())}d}' + pattern[hashes.end():]
    return Path(job['output']) / name

def valid_png(path):
    """Require complete chunks, valid CRCs, and the terminal IEND chunk."""
    try:
        with Path(path).open('rb') as f:
            if f.read(8) != b'\x89PNG\r\n\x1a\n':
                return False
            first = True
            image_data = False
            while True:
                header = f.read(8)
                if len(header) != 8:
                    return False
                size, kind = struct.unpack('>I4s', header)
                if size > 512 * 1024 * 1024 or (first and (kind != b'IHDR' or size != 13)):
                    return False
                first = False
                checksum = zlib.crc32(kind)
                remain = size
                while remain:
                    block = f.read(min(remain, 1024*1024))
                    if not block:
                        return False
                    checksum = zlib.crc32(block, checksum)
                    remain -= len(block)
                crc = f.read(4)
                if len(crc) != 4 or struct.unpack('>I', crc)[0] != checksum & 0xffffffff:
                    return False
                if kind == b'IDAT':
                    image_data = True
                if kind == b'IEND':
                    return size == 0 and image_data and not f.read(1)
    except (OSError, ValueError):
        return False

def inspect_frames(job, cache=None):
    cache = {} if cache is None else cache
    done = {}
    pattern = job['external']['pattern']
    hashes = re.search(r'#+', pattern)
    names = re.compile(re.escape(pattern[:hashes.start()])+r'(-?\d+)'+re.escape(pattern[hashes.end():]))
    frames = range(job['start'], job['end']+1, job['step'])
    try:
        entries = os.scandir(job['output'])
    except OSError:
        return done
    with entries:
      for entry in entries:
        match = names.fullmatch(entry.name)
        if not match: continue
        frame = int(match[1])
        if frame not in frames: continue
        path = frame_path(job, frame)
        if path.name != entry.name: continue
        try:
            before = entry.stat()
            signature = before.st_size, before.st_mtime_ns
            key = str(path)
            if cache.get(key, (None,))[0] != signature:
                valid = valid_png(path)
                after = path.stat()
                cache[key] = (signature, valid and signature == (after.st_size, after.st_mtime_ns))
            if cache[key][1]:
                done[str(frame)] = {'path':key, 'size':before.st_size, 'mtime_ns':before.st_mtime_ns}
        except OSError:
            pass
    return done
