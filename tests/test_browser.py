"""Browser regression test against the actual HTTP service, with isolated .blend probe fixtures."""
from pathlib import Path
import argparse
import copy
import tempfile
import threading
from unittest.mock import patch
from PIL import Image
from playwright.sync_api import sync_playwright, expect
from waitress import create_server
from renderdesk.runtime import Runtime
from renderdesk.server import create_app
from renderdesk.engine.protocol import write, signature, receipt
from tests.test_features import sample


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--screenshots',type=Path);args=parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory);fixture=sample(root);other=root/'夜景 · 动画.blend';other.touch()
        runtime=Runtime(root/'data');token='isolated-ui-test-key'
        runtime.registry.resolve=lambda _:str(root/'blender.exe')
        def probe(exe,blend):
            result=copy.deepcopy(fixture);result['blend']=str(blend);result['source']=signature(blend);return result
        server=create_server(create_app(runtime,token),host='127.0.0.1',port=0)
        threading.Thread(target=server.run,daemon=True).start();url=f'http://127.0.0.1:{server.effective_port}/'
        try:
            with patch('renderdesk.runtime.scan_project',side_effect=probe),sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1100});errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url+'#token='+token)
                expect(page.locator('html')).to_have_attribute('data-ready','true')
                # The same receiving function is called by native pywebview drop events.
                page.evaluate('paths=>window.receiveDroppedProjects(paths)',[fixture['blend'],str(other)])
                expect(page.locator('#project-paths')).to_have_value(fixture['blend']+'\n'+str(other))
                page.get_by_role('button',name='读取工程配置',exact=True).click()
                expect(page.locator('[data-scan]')).to_have_count(2,timeout=15000)
                expect(page.locator('.scan-meta').first).to_contain_text('1200')
                assert page.locator('[data-start]').first.is_disabled()
                page.get_by_role('button',name='加入渲染队列',exact=True).click()
                expect(page.locator('.task-card')).to_have_count(2,timeout=15000)
                assert all(t['status']['total']==1200 for t in runtime.snapshot()['tasks'])
                expect(page.locator('#detail-info')).to_contain_text('共 1200 帧')
                # Commit an illustrative finished frame, as the runner would.
                task=runtime.snapshot()['tasks'][-1];jid=task['job']['id'];path=root/'images/shot_0001.png';path.parent.mkdir()
                img=Image.new('RGB',(960,540),'#17352e');img.save(path)
                write(root/'data/jobs'/jid/'progress.json',{'done':{'1':receipt(path)}})
                expect(page.locator('#preview')).to_be_visible(timeout=10000)
                page.get_by_role('button',name='◷ 定时计划').click();page.locator('[name=start_at]').fill('2099-01-01T12:00')
                page.get_by_role('button',name='保存计划').click();expect(page.locator('#schedule-dialog')).not_to_be_visible()
                second=browser.new_page(viewport={'width':390,'height':844});second.goto(url)
                second.get_by_label('访问密钥',exact=True).fill(token);second.get_by_role('button',name='连接工作空间 →').click()
                expect(second.locator('.task-card')).to_have_count(2)
                assert second.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if args.screenshots:
                    args.screenshots.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(args.screenshots/'desktop.png'),full_page=True)
                    second.screenshot(path=str(args.screenshots/'mobile.png'),full_page=True)
                page.locator('[data-view=settings]').click();expect(page.locator('#watch-files')).to_be_checked()
                page.locator('#watch-recover').check();page.get_by_role('button',name='保存监测设置').click()
                expect(page.locator('#toast')).to_contain_text('Watchdog')
                page.locator('#manual-key').fill('new-manual-access-key-5678');page.get_by_role('button',name='更新密钥，立即生效').click()
                expect(page.locator('#toast')).to_contain_text('新密钥已生效')
                expect(second.locator('#login')).to_be_visible(timeout=8000)
                second.locator('#access-key').fill('new-manual-access-key-5678');second.get_by_role('button',name='连接工作空间 →').click()
                expect(second.locator('#login')).not_to_be_visible()
                page.locator('#bark-url').fill('https://api.day.app/ui-test-never-sent')
                page.locator('#bark-form').get_by_role('button',name='保存设置',exact=True).click()
                expect(page.locator('#bark-configured')).to_contain_text('已保存')
                page.locator('#remote-enabled').check();page.get_by_role('button',name='保存，重启后生效').click()
                expect(page.locator('#toast')).to_contain_text('下次启动')
                if args.screenshots:page.screenshot(path=str(args.screenshots/'settings.png'),full_page=True)
                assert not errors,errors
                browser.close();print('PASS browser: batch/drop receiver, scanned defaults 1200 frames, original output, preview, schedule, shared desktop/mobile, watchdog, key rotation, Bark/remote settings; no JS errors')
        finally:server.close();runtime.close()

if __name__=='__main__':main()
