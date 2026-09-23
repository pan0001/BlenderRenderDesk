"""Optional end-to-end frp test using a local frps, without an external server."""
import argparse
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
from waitress import create_server
from renderdesk.runtime import Runtime
from renderdesk.server import create_app
from renderdesk.engine.protocol import write


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--frps',type=Path,required=True);args=parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);runtime=Runtime(root/'data');key='loopback-test-access-key'
        server=create_server(create_app(runtime,key),host='127.0.0.1',port=0)
        threading.Thread(target=server.run,daemon=True).start()
        port,remote=free_port(),free_port()
        cfg=root/'frps.json';write(cfg,{'bindAddr':'127.0.0.1','bindPort':port,'proxyBindAddr':'127.0.0.1','auth':{'method':'token','token':'test-only-frp-token'}})
        write(runtime.root/'server-status.json',{'url':f'http://127.0.0.1:{server.effective_port}/'})
        with (root/'frps.log').open('wb') as log:
            child=subprocess.Popen([str(args.frps.resolve()),'-c',str(cfg)],stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            time.sleep(.4)
            runtime.submit('frpc.save',{'server':'127.0.0.1','port':port,'remote_port':remote,'name':'renderdesk-test','token':'test-only-frp-token'}).result(10)
            runtime.submit('frpc.start',{}).result(20)
            url=f'http://127.0.0.1:{remote}/api/state'
            for attempt in range(50):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+key}),timeout=1) as result:
                        assert result.status==200;break
                except (OSError,urllib.error.URLError):time.sleep(.1)
            else:raise AssertionError(runtime.tunnel.log())
            try:urllib.request.urlopen(url,timeout=2);raise AssertionError('unauthenticated access accepted')
            except urllib.error.HTTPError as error:assert error.code==401
            assert 'test-only-frp-token' not in str(runtime.tunnel.settings())
            runtime.submit('frpc.stop',{}).result(10)
            assert not runtime.tunnel.settings()['running']
            print('PASS official frpc verify, local frps TCP tunnel, authenticated HTTP 200 / unauthenticated 401, token redaction, clean stop')
        finally:
            runtime.close();server.close();child.terminate();child.wait(10)

if __name__=='__main__':main()
