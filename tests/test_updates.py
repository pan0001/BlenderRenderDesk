import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from types import SimpleNamespace

from renderdesk.updates import (Updater, version_tuple, trusted_url, release_info, verify_archive,
                                extract_archive, fetch, check_render_locations, RELEASES_URL, LATEST_URL, VERSION)
from renderdesk.server import create_app


def release(version='99.0.0'):
    tag = 'v' + version
    names = ['BlenderRenderDesk-' + tag + '-Windows.zip', 'SHA256SUMS-' + tag + '.txt']
    return {'tag_name': tag, 'body': '<script>not HTML</script>\n中文说明', 'assets': [
        {'name': name, 'state': 'uploaded', 'size': 100,
         'browser_download_url': RELEASES_URL + '/download/' + tag + '/' + name} for name in names]}


def bundle_bytes(version='99.0.0'):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as archive:
        archive.writestr('BlenderRenderDesk/BlenderRenderDesk.exe', b'fixture exe')
        archive.writestr('BlenderRenderDesk/_internal/library', b'fixture lib')
        archive.writestr('BlenderRenderDesk/VERSION', version)
    return result.getvalue()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stable_semver_and_no_downgrade(self):
        self.assertGreater(version_tuple('v3.10.0'), version_tuple('3.9.9'))
        for bad in ('v3.5.0-beta', '../3.5.0', '3.05.0', '3.5', None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                version_tuple(bad)
        for version in ('1.0.0', VERSION):
            self.assertIsNone(release_info(release(version)))
        self.assertEqual(release_info(release())['version'], '99.0.0')

    def test_incomplete_or_untrusted_release(self):
        for change in ('missing', 'prerelease', 'wrong-repo', 'oversized'):
            data = release()
            if change == 'missing': data['assets'].pop()
            if change == 'prerelease': data['prerelease'] = True
            if change == 'wrong-repo': data['assets'][0]['browser_download_url'] = 'https://github.com/evil/fake/download.zip'
            if change == 'oversized': data['assets'][0]['size'] = 2**40
            with self.subTest(change=change), self.assertRaises(ValueError):
                release_info(data)

    def test_redirects_cannot_escape_official_hosts(self):
        for url in ('http://github.com/test', 'https://github.com.evil.test/x',
                    'https://user@github.com/' + 'pan0001/BlenderRenderDesk/releases/download/x/y',
                    'https://127.0.0.1/a', 'file:///C:/secret', 'https://github.com/other/repo/releases/download/x/y'):
            with self.subTest(url=url), self.assertRaises(ValueError): trusted_url(url)
        self.assertEqual(trusted_url(LATEST_URL), LATEST_URL)
        self.assertTrue(trusted_url('https://release-assets.githubusercontent.com/a?signature=x'))

    def test_sha256_size_and_github_digest(self):
        archive = self.root / 'update.zip'
        archive.write_bytes(bundle_bytes())
        info = release_info(release())
        info['size'] = archive.stat().st_size
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = digest + '  ' + info['name'] + '\n'
        self.assertEqual(verify_archive(archive, info, checksum), digest)
        info['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(ValueError): verify_archive(archive, info, checksum)
        info['digest'] = 'sha256:' + digest
        with self.assertRaises(ValueError): verify_archive(archive, info, checksum + checksum)
        archive.write_bytes(archive.read_bytes() + b'corruption')
        with self.assertRaises(ValueError): verify_archive(archive, info, checksum)

    def test_path_traversal_symlinks_duplicate_and_ntfs_paths_rejected(self):
        bad_names = ['../escape', 'BlenderRenderDesk/../../escape', '/BlenderRenderDesk/x',
                     'BlenderRenderDesk/C:/escape', 'BlenderRenderDesk/CON.txt',
                     'BlenderRenderDesk/x:ads', 'BlenderRenderDesk/x.', 'BlenderRenderDesk\\escape']
        for name in bad_names:
            archive = self.root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z: z.writestr(name, b'x')
            with self.subTest(name=name), self.assertRaises(ValueError):
                extract_archive(archive, self.root, '99.0.0')
        with zipfile.ZipFile(archive, 'w') as z:
            member = zipfile.ZipInfo('BlenderRenderDesk/link')
            member.create_system = 3
            member.external_attr = (0o120777 << 16)
            z.writestr(member, '../../outside')
        with self.assertRaises(ValueError): extract_archive(archive, self.root, '99.0.0')
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('BlenderRenderDesk/FILE', b'a')
            z.writestr('BlenderRenderDesk/file', b'b')
        with self.assertRaises(ValueError): extract_archive(archive, self.root, '99.0.0')

    def test_extract_checks_version_and_required_binary(self):
        archive = self.root / 'update.zip'
        archive.write_bytes(bundle_bytes())
        app = extract_archive(archive, self.root, '99.0.0')
        self.assertEqual((app / 'BlenderRenderDesk.exe').read_bytes(), b'fixture exe')
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaises(ValueError): extract_archive(archive, other, '99.1.0')

    def test_check_download_state_and_preserve_task_data(self):
        updater = Updater(self.root / 'data')
        updater.install = self.root / 'installed'
        updater.install.mkdir()
        updater.supported = True
        sentinel = updater.root / 'user-task.json'
        sentinel.write_text('keep this', encoding='utf8')
        binary = bundle_bytes()
        data = release()
        data['assets'][0]['size'] = len(binary)
        digest = hashlib.sha256(binary).hexdigest()
        def fake_fetch(url, limit, destination=None, progress=None, cancel=None):
            if url == LATEST_URL: return json.dumps(data).encode()
            if destination:
                destination.write_bytes(binary)
                progress(len(binary))
                return len(binary)
            return (digest + '  ' + data['assets'][0]['name']).encode()
        try:
            with patch('renderdesk.updates.fetch', side_effect=fake_fetch):
                updater.check(); updater.thread.join(5)
                self.assertEqual(updater.snapshot()['phase'], 'available')
                updater.download(); updater.thread.join(5)
                self.assertEqual(updater.snapshot()['phase'], 'ready')
                self.assertTrue(updater.staged.is_dir())
                self.assertEqual(sentinel.read_text(), 'keep this')
                self.assertFalse((updater.install / 'BlenderRenderDesk.exe').exists())
                self.assertEqual(updater.snapshot()['received'], len(binary))
        finally:
            updater.close()
        self.assertEqual(sentinel.read_text(), 'keep this')
        self.assertEqual(list(self.root.glob('.renderdesk-update-*')), [])

    def test_failed_download_keeps_install_and_can_retry(self):
        updater = Updater(self.root / 'data')
        updater.install = self.root / 'installed'
        updater.install.mkdir()
        old = updater.install / 'BlenderRenderDesk.exe'
        old.write_bytes(b'old version')
        updater.supported = True
        updater.info = release_info(release())
        try:
            with patch('renderdesk.updates.fetch', side_effect=OSError('network failed')):
                updater.download(); updater.thread.join(5)
                self.assertEqual(updater.snapshot()['phase'], 'error')
                self.assertEqual(old.read_bytes(), b'old version')
                with self.assertRaises(ValueError): updater.install_update()
            with patch('renderdesk.updates.fetch', return_value=json.dumps(release('1.0.0')).encode()):
                updater.check(); updater.thread.join(5)
                self.assertEqual(updater.snapshot()['phase'], 'current')
        finally: updater.close()

    def test_check_runs_off_thread_and_rejects_concurrent_operations(self):
        updater = Updater(self.root)
        release_gate = threading.Event()
        def blocked(*args):
            release_gate.wait(5)
            return json.dumps(release()).encode()
        try:
            with patch('renderdesk.updates.fetch', side_effect=blocked):
                state = updater.check()
                self.assertEqual(state['phase'], 'checking')
                with self.assertRaises(ValueError): updater.check()
                release_gate.set()
                updater.thread.join(5)
        finally: updater.close()

    def test_source_mode_rejects_install_and_settings_persist(self):
        updater = Updater(self.root)
        self.assertFalse(updater.supported)
        with self.assertRaises(ValueError): updater.download()
        with self.assertRaises(ValueError): updater.configure('false')
        updater.configure(False)
        self.assertFalse(Updater(self.root).snapshot()['auto_check'])

    def test_update_api_requires_auth_and_does_not_accept_arbitrary_urls(self):
        updater = Updater(self.root)
        runtime = SimpleNamespace(root=self.root)
        client = create_app(runtime, 'test-secret', updater=updater).test_client()
        headers = {'Authorization': 'Bearer test-secret'}
        self.assertEqual(client.get('/api/updates').status_code, 401)
        self.assertEqual(client.post('/api/updates', json={'action':'download'}).status_code, 401)
        self.assertEqual(client.post('/api/updates', json={'action':'settings','auto_check':False}, headers={**headers, 'Origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(client.post('/api/updates', json={'action':'https://evil.test/update'}, headers=headers).status_code, 400)
        result = client.post('/api/updates', json={'action':'settings','auto_check':False}, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json['auto_check'])
        self.assertEqual(client.get('/api/updates', headers=headers).json['current'], VERSION)

    def test_streaming_limits_and_cancel(self):
        class Response(io.BytesIO):
            headers = {}
        opener = SimpleNamespace(open=lambda *args, **kwargs: Response(b'x' * 1024))
        with patch('renderdesk.updates.build_opener', return_value=opener):
            with self.assertRaises(ValueError): fetch(LATEST_URL, 32)
            cancel = threading.Event(); cancel.set()
            with self.assertRaises(ValueError): fetch(LATEST_URL, 2048, cancel=cancel)

    def test_protect_projects_stored_inside_application_directory(self):
        app = self.root / 'app'
        safe = {'tasks': [{'job': {'output': str(self.root / 'frames')}}]}
        check_render_locations(app, safe)
        for job in ({'output': str(app / 'frames')}, {'blend': str(app / 'scene.blend')},
                    {'external': {'script': str(app / 'render.py')}}, {'batch': {'cwd': str(app)}}):
            with self.subTest(job=job), self.assertRaises(ValueError):
                check_render_locations(app, {'tasks': [{'job': job}]})

    def test_install_handoff_pins_data_directory_and_does_not_stop_blender(self):
        updater = Updater(self.root / 'data', shutdown=lambda: None)
        updater.install = self.root / 'install'
        updater.install.mkdir()
        updater.supported = True
        updater.stage_root = self.root / '.renderdesk-update-unit'
        updater.staged = updater.stage_root / 'BlenderRenderDesk'
        updater.staged.mkdir(parents=True)
        updater.info = release_info(release())
        updater.state['phase'] = 'ready'
        with patch('renderdesk.updates.sys.argv', ['app', '--server', '--port', '8765', '--data-dir=old', '--update-ready', 'old-ack']), \
                patch('renderdesk.updates.subprocess.Popen') as launch, patch('renderdesk.updates.threading.Timer') as timer:
            self.assertEqual(updater.install_update()['phase'], 'installing')
            launch.assert_called_once()
            command = launch.call_args.args[0]
            self.assertEqual(command[-2], '-Plan')
            plan = json.loads(Path(command[-1]).read_text(encoding='utf8'))
            self.assertNotIn('old-ack', plan['updated_arguments'])
            self.assertNotIn('--data-dir=old', plan['updated_arguments'])
            self.assertIn('--server --port 8765', plan['arguments'])
            self.assertIn(str(updater.root), plan['arguments'])
            timer.return_value.start.assert_called_once()
            with self.assertRaises(ValueError): updater.install_update()
        updater.close()
        self.assertTrue(updater.staged.exists(), 'Shutdown must leave installation staging intact')


if __name__ == '__main__':
    unittest.main()
