import json
from pathlib import Path
import tempfile
import time
import unittest
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from renderdesk.runtime import Runtime
from renderdesk.server import create_app
from renderdesk.notifications import Notifications, validate_url, send_bark


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Runtime(self.root)
        self.app = create_app(self.runtime, 'test-secret')
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer test-secret'}

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_auth_and_cross_origin(self):
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        self.assertEqual(self.client.get('/api/state', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post('/api/commands', json={'action':'refresh'}, headers={**self.headers, 'Origin':'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/access').status_code, 401)
        self.assertEqual(self.client.get('/assets/../../bark.json').status_code, 404)

    def test_static_and_csp(self):
        response = self.client.get('/')
        self.assertIn(b'Bark', response.data)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        response.close()
        with self.client.get('/assets/app.js') as asset:
            self.assertEqual(asset.status_code, 200)

    def test_commands_validate_and_shared_state(self):
        blend, exe = self.root / 'a.blend', self.root / 'blender.exe'
        blend.touch(); exe.touch()
        response = self.client.post('/api/commands', json={'action':'add','values':{'blend':str(blend),'blender':str(exe),'output':str(self.root/'renders'),'start':1,'end':2,'step':1}}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        ident = response.json['operation']
        for _ in range(100):
            result = self.client.get('/api/operations/'+ident, headers=self.headers).json
            if result['done']: break
            time.sleep(.03)
        self.assertNotIn('error', result)
        jid = result['result']
        second_client = self.app.test_client()
        self.assertEqual(second_client.get('/api/state', headers=self.headers).json['tasks'][0]['job']['id'], jid)
        # Independent cursors: one viewer cannot consume another viewer's logs.
        path = self.root/'jobs'/jid/'render.log'
        path.write_bytes(('中文日志\n'*10000).encode())
        a = self.client.get(f'/api/jobs/{jid}/log?cursor=-1',headers=self.headers).json
        b = second_client.get(f'/api/jobs/{jid}/log?cursor=-1',headers=self.headers).json
        self.assertEqual(a,b)
        self.assertLessEqual(len(a['text']),32768)
        bad = self.client.post('/api/commands',json={'action':'__dict__'},headers=self.headers)
        self.assertEqual(bad.status_code,400)

    def test_bark_secret_not_returned(self):
        self.runtime.submit('bark.save',{'enabled':True,'url':'https://api.day.app/private-secret'}).result(5)
        response=self.client.get('/api/settings',headers=self.headers)
        self.assertTrue(response.json['bark']['configured'])
        self.assertNotIn(b'private-secret',response.data)


class NotificationTests(unittest.TestCase):
    def test_real_http_payload_and_application_error(self):
        received=[]
        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200);self.end_headers()
                self.wfile.write(b'{"code":200}' if len(received)==1 else b'{"code":400}')
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Endpoint)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/test-device'
            send_bark(url,'渲染完成','中文任务，12 帧')
            self.assertEqual(received[0][0],'/test-device')
            self.assertEqual(received[0][1]['body'],'中文任务，12 帧')
            with self.assertRaises(ValueError):send_bark(url,'test','test')
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_completion_dedup_restart_and_no_historical_flood(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            sent=[]
            n=Notifications(root,sender=lambda *args:sent.append(args))
            n.configure({'enabled':True,'url':'https://api.day.app/test-key'})
            def snapshot(state):
                return {'tasks':[{'job':{'id':'a','name':'中文任务'},'status':{'state':state,'done':2,'total':2,'elapsed':12}}]}
            n.observe(snapshot('complete'))
            self.assertEqual(n.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
            n.observe(snapshot('rendering'))
            n.observe(snapshot('paused'))
            n.observe(snapshot('complete'))
            n.observe(snapshot('complete'))
            for _ in range(30):
                if sent:break
                time.sleep(.1)
            self.assertEqual(len(sent),1)
            n.close()
            n=Notifications(root,sender=lambda *args:sent.append(args))
            n.observe(snapshot('complete'))
            self.assertEqual(n.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
            n.close()

    def test_failure_retries_without_secret_error(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail(*args):raise RuntimeError('https://api.day.app/SECRET')
            n=Notifications(Path(directory),sender=fail)
            n.configure({'enabled':True,'url':'https://api.day.app/SECRET'})
            n.test()
            for expected in (1,2,3):
                for _ in range(30):
                    if n.settings()['attempts']>=expected:break
                    time.sleep(.1)
                self.assertEqual(n.settings()['attempts'],expected)
                with n.db:n.db.execute('UPDATE outbox SET due=0')
            self.assertEqual(n.settings()['last_status'],'failed')
            self.assertNotIn('SECRET',json.dumps(n.settings()))
            n.close()

    def test_invalid_push_url(self):
        for value in ['file:///secret','https://api.day.app','https://user:pass@host/key','https://host/key?query=1']:
            with self.assertRaises(ValueError):validate_url(value)


if __name__=='__main__':unittest.main()
