"""Optional UI test: pip install playwright; uses an installed Edge browser."""
from pathlib import Path
import argparse
import tempfile
import threading
import time
from playwright.sync_api import sync_playwright, expect
from waitress import create_server
from renderdesk.runtime import Runtime
from renderdesk.server import create_app
from renderdesk.engine.protocol import write


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--screenshots',type=Path)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)
        runtime=Runtime(root)
        token='isolated-ui-test-key'
        server=create_server(create_app(runtime,token),host='127.0.0.1',port=0)
        threading.Thread(target=server.run,daemon=True).start()
        url=f'http://127.0.0.1:{server.effective_port}/'
        try:
            blend=root/'晨光 · 室内空间.blend';blend.touch()
            exe=root/'blender.exe';exe.touch()
            jid=runtime.submit('add',{'blend':str(blend),'blender':str(exe),'output':str(root/'renders'),'start':1,'end':120,'step':1}).result(5)
            with (root/'jobs'/jid/'render.log').open('a',encoding='utf8') as stream:stream.write('\n[RenderDesk] 工程已准备就绪。点击开始 / 继续启动 Blender。\n')
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1100})
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(url+'#token='+token)
                expect(page.locator("html")).to_have_attribute("data-ready", "true")
                page.get_by_role('button',name='＋ 新建任务',exact=True).click()
                page.locator('[name=blender]').fill(str(exe))
                page.locator('[name=blend]').fill(str(blend))
                page.locator('[name=output]').fill(str(root/'renders'))
                page.get_by_role('button',name='创建任务',exact=True).click()
                expect(page.locator(".task-card")).to_have_count(2)
                assert len(runtime.snapshot()['tasks'])==2
                # A remote client starts with the login UI and sees the same two tasks.
                second=browser.new_page(viewport={'width':390,'height':844})
                second.goto(url)
                second.get_by_label('访问密钥',exact=True).fill(token)
                second.get_by_role('button',name='连接工作空间 →').click()
                expect(second.locator("html")).to_have_attribute("data-ready", "true")
                assert second.locator('.task-card').count()==2
                assert second.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_role('button',name='◷ 定时计划').click()
                page.locator('[name=start_at]').fill('2099-01-01T12:00')
                page.get_by_role('button',name='保存计划').click()
                expect(page.locator("#schedule-dialog")).not_to_be_visible()
                assert any(t['job']['start_at'] for t in runtime.snapshot()['tasks'])
                page.get_by_role('button',name='⚙ 连接与通知').click()
                page.wait_for_selector('#bark-configured')
                page.locator('#bark-url').fill('https://api.day.app/ui-test-never-sent')
                page.locator('#bark-form').get_by_role('button',name='保存设置',exact=True).click()
                expect(page.locator("#bark-configured")).to_contain_text("已保存")
                page.locator('#remote-enabled').check()
                page.get_by_role('button',name='保存，重启后生效').click()
                expect(page.locator("#toast")).to_contain_text("下次启动")
                assert (root/'server.json').exists()
                if args.screenshots:
                    args.screenshots.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(args.screenshots/'settings.png'),full_page=True)
                    page.locator('[data-view=tasks]').click()
                    page.screenshot(path=str(args.screenshots/'desktop.png'),full_page=True)
                    second.screenshot(path=str(args.screenshots/'mobile.png'),full_page=True)
                assert not errors,errors
                browser.close()
                print('PASS: desktop/mobile layouts, auth, shared tasks, create task, schedule, Bark/remote settings; no JS errors')
        finally:
            server.close();runtime.close()


if __name__=='__main__':main()
