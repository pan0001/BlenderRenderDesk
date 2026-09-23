"""Process identity, resource sampling and optional Windows GPU counters."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import shutil
import time
import psutil


def process(identity):
    try:
        item = psutil.Process(int(identity['pid']))
        if item.is_running() and abs(item.create_time() - identity['created']) < .01 and item.status() != psutil.STATUS_ZOMBIE:
            return item
    except (psutil.Error, KeyError, TypeError, ValueError):
        pass
    return None


def installations():
    base = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Blender Foundation'
    found = [str(p) for p in sorted(base.glob('Blender */blender.exe'), reverse=True)]
    found += [shutil.which('blender')] if shutil.which('blender') else []
    return list(dict.fromkeys(found))


class GpuCounters:
    """PDH names are English so localized Windows installations also work."""
    def __init__(self):
        self.query, self.counters = ctypes.c_void_p(), {}
        self.api = None
        if os.name != 'nt':
            return
        try:
            self.api = ctypes.WinDLL('pdh.dll')
            self.api.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.api.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            self.api.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            self.api.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            self.api.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            if self.api.PdhOpenQueryW(None, 0, ctypes.byref(self.query)):
                return
            for name, path in [('gpu', r'\GPU Engine(*)\Utilization Percentage'), ('vram_mb', r'\GPU Process Memory(*)\Dedicated Usage')]:
                counter = ctypes.c_void_p()
                if self.api.PdhAddEnglishCounterW(self.query, path, 0, ctypes.byref(counter)) == 0:
                    self.counters[name] = counter
            self.api.PdhCollectQueryData(self.query)
        except (OSError, AttributeError):
            self.close()

    def sample(self):
        result = {}
        if not self.api or not self.query:
            return result
        if self.api.PdhCollectQueryData(self.query):
            return result

        class Value(ctypes.Structure):
            _fields_ = [('status', wintypes.DWORD), ('number', ctypes.c_double)]

        class Item(ctypes.Structure):
            _fields_ = [('name', wintypes.LPWSTR), ('value', Value)]

        for metric, counter in self.counters.items():
            size, count = wintypes.DWORD(), wintypes.DWORD()
            self.api.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size), ctypes.byref(count), None)
            if not size.value or size.value > 32 * 1024 * 1024:
                continue
            buffer = ctypes.create_string_buffer(size.value)
            if self.api.PdhGetFormattedCounterArrayW(counter, 0x200, ctypes.byref(size), ctypes.byref(count), buffer):
                continue
            items = ctypes.cast(buffer, ctypes.POINTER(Item))
            for index in range(count.value):
                item = items[index]
                match = re.search(r'(?:^|_)pid_(\d+)', item.name or '')
                if not match or item.value.status not in (0, 1):
                    continue
                row = result.setdefault(int(match[1]), {})
                number = max(0, item.value.number)
                if metric == 'gpu':
                    row[metric] = max(row.get(metric, 0), min(number, 100))
                else:
                    row[metric] = row.get(metric, 0) + number / 1048576
        return result

    def close(self):
        if self.api and self.query:
            self.api.PdhCloseQuery(self.query)
            self.query = ctypes.c_void_p()


class Sampler:
    def __init__(self):
        self.cache = {}
        self.gpu = GpuCounters()
        psutil.cpu_percent()

    def scan(self):
        rows, identities = [], set()
        gpu = self.gpu.sample()
        for item in psutil.process_iter(['name']):
            if (item.info['name'] or '').lower() not in ('blender', 'blender.exe'):
                continue
            try:
                created = item.create_time()
                key = (item.pid, created)
                identities.add(key)
                first = key not in self.cache
                cached = self.cache.setdefault(key, item)
                with cached.oneshot():
                    args = cached.cmdline()
                    cpu = cached.cpu_percent() / (psutil.cpu_count() or 1)
                    scheduler_script = None
                    try:
                        from .batch import python_script
                        parent = cached.parent()
                        path = python_script(parent) if parent else None
                        scheduler_script = str(path) if path else None
                    except (psutil.Error, OSError):
                        pass
                    rows.append({'pid': item.pid, 'created': created, 'exe': cached.exe(), 'args': args,
                                 'scheduler_script': scheduler_script,
                                 'cwd': cached.cwd(), 'blend': next((a for a in args if a.lower().endswith('.blend')), ''),
                                 'cpu': None if first else round(cpu, 1), 'ram_mb': cached.memory_info().rss / 1048576,
                                 'elapsed': max(0, time.time() - created), **gpu.get(item.pid, {})})
            except psutil.Error:
                continue
        self.cache = {key: value for key, value in self.cache.items() if key in identities}
        ram = psutil.virtual_memory()
        return rows, {'cpu': psutil.cpu_percent(), 'ram_used': ram.used / 1073741824, 'ram_total': ram.total / 1073741824}

    def close(self):
        self.gpu.close()
