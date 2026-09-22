"""Serial background operations with immutable snapshots for the Tk thread."""
import codecs
import copy
import queue
import os
from concurrent.futures import ThreadPoolExecutor


class LogStream:
    """Read appended bytes only; bound catch-up work and preserve split UTF-8."""
    def __init__(self):
        self.job_id = None
        self.offset = 0
        self.identity = None
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def read(self, manager, job_id, limit=32_768):
        changed = job_id != self.job_id
        if changed:
            self.job_id = job_id
            self.offset = 0
            self.identity = None
            self.decoder.reset()
        if not job_id:
            return None
        path = manager.directory(job_id)/'render.log'
        try:
            with path.open('rb') as f:
                stat = os.fstat(f.fileno())
                identity = (stat.st_dev, stat.st_ino)
                reset = changed or identity != self.identity or stat.st_size < self.offset
                if reset:
                    self.offset = 0
                    self.decoder.reset()
                self.identity = identity
                if stat.st_size-self.offset > limit:
                    self.offset = stat.st_size-limit
                    self.decoder.reset()
                    reset = True
                    f.seek(self.offset)
                    # Skip at most three continuation bytes at a UTF-8 boundary.
                    lead = f.read(3)
                    skipped = 0
                    for byte in lead:
                        if byte & 0xc0 != 0x80: break
                        skipped += 1
                    self.offset += skipped
                f.seek(self.offset)
                data = f.read(limit)
                self.offset += len(data)
                text = self.decoder.decode(data)
                if not text and not reset:
                    return None
                return {'job_id':job_id,'reset':reset,'text':text}
        except FileNotFoundError:
            if changed or self.identity is not None:
                self.identity = None
                self.offset = 0
                self.decoder.reset()
                return {'job_id':job_id,'reset':True,'text':''}
            return None


class BackgroundManager:
    """Only the worker touches the real manager; UI reads cached snapshots."""
    def __init__(self, manager, messages=None):
        self.backend = manager
        self.root = manager.root
        self.errors = manager.errors
        self.jobs = copy.deepcopy(manager.jobs)
        self.states = {}
        self.log_chunk = None
        self.messages = messages if messages is not None else queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix='renderdesk')
        self.refresh_pending = False
        self.operations = 0
        self.closed = False
        self.stream = LogStream()

    def directory(self, job_id):
        return self.root/'jobs'/job_id

    def status(self, job_id):
        return self.states.get(job_id,{'state':'ready','running':False,'done':0})

    def live(self, job_id):
        return self.status(job_id)['running']

    def snapshot(self, selected, notices=None):
        return {'jobs':copy.deepcopy(self.backend.jobs),
                'states':{jid:self.backend.status(jid) for jid in self.backend.jobs},
                'log':self.stream.read(self.backend,selected), 'notices':notices or []}

    def apply(self, snapshot):
        self.jobs = snapshot['jobs']
        self.states = snapshot['states']
        if snapshot['log'] is not None:
            self.log_chunk = snapshot['log']

    def request_refresh(self, selected=None):
        if self.closed or self.refresh_pending or self.operations:
            return
        self.refresh_pending = True
        def work():
            try:
                notices = self.backend.tick()
                snapshot = self.snapshot(selected,notices)
                self.messages.put(('backend_snapshot',snapshot))
            except Exception as e:
                self.messages.put(('backend_error',str(e)))
        self.executor.submit(work)

    def submit(self, operation, callback=None, selected=None):
        if self.closed: return
        self.operations += 1
        def work():
            result = None
            error = None
            snapshot = None
            try:
                result = operation(self.backend)
            except Exception as e:
                error = str(e)
            try:
                jid = result['id'] if isinstance(result,dict) and 'id' in result else selected
                snapshot = self.snapshot(jid)
            except Exception as e:
                error = error or str(e)
            self.messages.put(('backend_operation',(snapshot,result,error,callback)))
        self.executor.submit(work)

    def take_log(self, job_id):
        chunk = self.log_chunk
        if chunk and chunk['job_id'] == job_id:
            self.log_chunk = None
            return chunk
        return None

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=False,cancel_futures=True)
