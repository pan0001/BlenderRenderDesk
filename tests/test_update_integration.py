"""Windows release replacement / rollback against disposable copies and real Blender.

Run: python -m tests.test_update_integration --bundle path/to/BlenderRenderDesk
Never opens or modifies the user's running manager, processes, or task directory.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

import psutil
from renderdesk.adoption import infer_script
from renderdesk.engine.protocol import read, write
from renderdesk.processes import installations
from renderdesk.projects import scan_project
from renderdesk.service import Controller
from renderdesk.version import VERSION


def until(callback, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = callback()
            if result:
                return result
        except (OSError, ValueError, KeyError, StopIteration):
            pass
        time.sleep(.15)
    raise AssertionError('Timed out waiting for update fixture')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args()
    assert os.name == 'nt'
    root = Path(tempfile.mkdtemp(prefix='RenderDesk 更新 测试 '))
    install = root / 'app'
    shutil.copytree(args.bundle, install)
    data = root / 'data kept'
    write(data / 'updates/settings.json', {'auto_check': False})
    write(data / 'access-token.json', {'token': 'test-update-integration'})
    write(data / 'sentinel.json', {'keep': '中文数据'})
    blender = installations()[0]
    blend = root / 'scene.blend'
    setup = root / 'setup.py'
    setup.write_text("import bpy,sys\ns=bpy.context.scene;s.frame_start=1;s.frame_end=120;s.render.engine='CYCLES';s.cycles.samples=1;s.render.resolution_x=16;s.render.resolution_y=16;s.render.resolution_percentage=100\nbpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])", encoding='utf8')
    subprocess.run([blender, '-b', '--factory-startup', '--python', str(setup), '--', str(blend)],
                   check=True, stdout=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    script = root / 'render.py'
    script.write_text('''import bpy,time
from pathlib import Path
s=bpy.context.scene;s.render.image_settings.file_format='PNG'
out=Path(__file__).parent/'frames';out.mkdir(exist_ok=True)
for frame in range(1,121):
 s.frame_set(frame);s.render.filepath=str(out/f'{frame:04d}.png');bpy.ops.render.render(write_still=True)
 time.sleep(.5)
''', encoding='utf8')
    command = [blender, '-b', str(blend), '-t', '1', '--python', str(script)]
    child = subprocess.Popen(command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    owned_managers = []
    try:
        until(lambda: list((root / 'frames').glob('*.png')))
        values = infer_script(script, command, root, scan_project(blender, blend))
        values['process'] = {'pid': child.pid, 'created': psutil.Process(child.pid).create_time()}
        controller = Controller(data)
        jid = controller.attach(values)
        controller.close()
        # Port 0 verifies that launch arguments and custom data-dir survive replacement.
        launch_args = ([] if args.desktop else ['--server']) + ['--port', '0', '--data-dir', str(data)]
        initial = subprocess.Popen([str(install / 'BlenderRenderDesk.exe'), *launch_args], cwd=install,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        owned_managers.append(psutil.Process(initial.pid))
        status = until(lambda: read(data / 'server-status.json'))
        def api_state(status):
            with urlopen(Request(status['url'] + 'api/state', headers={'Authorization':'Bearer test-update-integration'}), timeout=5) as response:
                return json.load(response)
        until(lambda: api_state(status)['tasks'])
        for mode in ('success', 'rollback'):
            old_pid = status['pid']
            before = len(list((root / 'frames').glob('*.png')))
            stage_root = root / ('.renderdesk-update-' + mode)
            staged = stage_root / 'BlenderRenderDesk'
            shutil.copytree(args.bundle, staged)
            if mode == 'rollback':
                # A launchable new process that fails before readiness (headless, no error dialog).
                # The console fixture rejects the manager flags and exits nonzero.
                shutil.copyfile(Path(os.environ['SystemRoot']) / 'System32/where.exe', staged / 'BlenderRenderDesk.exe')
            ready = data / (mode + '-ready.json')
            result = data / (mode + '-result.json')
            backup = root / ('backup-' + mode)
            plan = dict(pid=old_pid, birth=psutil.Process(old_pid).create_time(), install=str(install),
                        staged=str(staged), stage_root=str(stage_root), backup=str(backup), version=VERSION,
                        ready=str(ready), result=str(result), arguments=subprocess.list2cmdline(launch_args),
                        updated_arguments=subprocess.list2cmdline(launch_args + ['--update-ready', str(ready)]))
            plan_file = data / (mode + '-plan.json')
            write(plan_file, plan)
            helper_path = Path(__file__).resolve().parents[1] / 'renderdesk/engine/install_update.ps1'
            helper = subprocess.Popen(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                       '-File', str(helper_path), '-Plan', str(plan_file)], cwd=data,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            # Only this disposable manager is stopped. The renderer must retain its PID.
            psutil.Process(old_pid).terminate()
            output, _ = helper.communicate(timeout=50)
            outcome = read(result)
            expected = 'installed' if mode == 'success' else 'failed'
            assert outcome['status'] == expected, (outcome, output)
            status = until(lambda: (s if (s := read(data / 'server-status.json')) and s['pid'] != old_pid else None))
            owned_managers.append(psutil.Process(status['pid']))
            state = until(lambda: api_state(status))
            task = next(t for t in state['tasks'] if t['job']['id'] == jid)
            assert task['status']['running'] and task['status']['pid'] == child.pid, task
            assert child.poll() is None
            until(lambda: len(list((root / 'frames').glob('*.png'))) > before)
            assert read(data / 'sentinel.json') == {'keep': '中文数据'}
            assert read(data / 'access-token.json')['token'] == 'test-update-integration'
            assert (install / 'BlenderRenderDesk.exe').read_bytes() == (args.bundle / 'BlenderRenderDesk.exe').read_bytes()
            if mode == 'success': assert backup.is_dir() and ready.is_file()
            print('PASS:', mode, '| same Blender PID, frames increasing, same job ID, settings and data kept', flush=True)
        print(root, flush=True)
    finally:
        for manager in owned_managers:
            try:
                manager.terminate(); manager.wait(10)
            except psutil.Error:
                pass
        if child.poll() is None:
            child.terminate()
        child.wait(15)


if __name__ == '__main__':
    main()
