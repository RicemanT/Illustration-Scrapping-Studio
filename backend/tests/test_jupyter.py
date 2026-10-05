import ast
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import jupyter_support as support


class JupyterLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notebook = json.loads((ROOT / 'Jupyter Studio.ipynb').read_text(encoding='utf-8'))

    def cell(self, name):
        return ''.join(next(c for c in self.notebook['cells'] if c['id'] == name)['source'])

    def archive(self, files):
        target = self.root / 'ui.zip'
        with zipfile.ZipFile(target, 'w') as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        return target

    def test_fresh_no_node_setup_extracts_before_setup_and_skips_git_for_archive(self):
        archive = self.archive({'dist/index.html': '<script src="/assets/new.js"></script>', 'dist/assets/new.js': 'new'})
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            self.assertTrue((self.root / 'frontend/dist/index.html').is_file())
        scope = dict(studio_support=support, APP=self.root, PORT=12345, UI_ZIP=archive, UPDATE_SOURCE=True, sys=sys, subprocess=subprocess)
        import shutil
        scope['shutil'] = shutil
        with patch.object(support, 'assert_stopped'), patch.object(shutil, 'which', return_value=None), patch.object(subprocess, 'run', side_effect=run):
            exec(self.cell('studio-3'), scope)
        self.assertEqual(len(calls), 1)
        self.assertIn('--skip-ui-build', calls[0])

    def test_stop_then_start_guard_and_repeated_stop(self):
        process = Mock()
        process.poll.return_value = None
        scope = dict(studio_support=support, studio_process=process)
        code = self.cell('studio-5').replace('STOP_APP = False', 'STOP_APP = True')
        exec(code, scope)
        exec(code, scope)
        process.send_signal.assert_called_once()
        guard = next(n for n in ast.parse(self.cell('studio-4')).body if isinstance(n, ast.If))
        exec(compile(ast.Module(body=[guard], type_ignores=[]), '<start guard>', 'exec'), scope)

    def test_timeout_never_kills_or_continues_update(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('app', 60)
        with self.assertRaisesRegex(RuntimeError, 'Shutdown is still'):
            support.stop_app(process)
        process.kill.assert_not_called()

    def test_surviving_process_blocks_update_before_mutations(self):
        scope = dict(studio_support=support, APP=self.root, PORT=12345)
        with patch.object(support, 'app_processes', return_value=[321]), patch.object(support, 'refresh_ui') as refresh:
            with self.assertRaisesRegex(RuntimeError, '321'):
                exec(self.cell('studio-3'), scope)
            refresh.assert_not_called()

    def test_occupied_port_blocks_start(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with patch.object(support, 'app_processes', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, 'occupied'):
                    support.assert_stopped(self.root, listener.getsockname()[1])

    def test_process_detection_ignores_argument_order(self):
        proc = self.root / 'proc' / '321'
        proc.mkdir(parents=True)
        command = ['python', str(self.root / 'studio.py'), 'run', '--library', '/data', '--port', '9000']
        (proc / 'cmdline').write_bytes('\0'.join(command).encode())
        self.assertEqual(support.app_processes(self.root, self.root / 'proc'), [321])

    def test_bad_archives_preserve_previous_ui(self):
        dist = self.root / 'frontend/dist'
        dist.mkdir(parents=True)
        (dist / 'index.html').write_text('old')
        for files in ({'dist/index.html': '<script src="/assets/missing.js"></script>'}, {'dist/../../escape': 'bad'}, {'readme.txt': 'missing UI'}):
            with self.assertRaises(ValueError):
                support.refresh_ui(self.root, self.archive(files))
            self.assertEqual((dist / 'index.html').read_text(), 'old')
        support.refresh_ui(self.root, self.archive({'dist/index.html': 'new'}))
        self.assertEqual((dist / 'index.html').read_text(), 'new')

    def test_common_zip_layouts_and_windows_separators(self):
        for prefix in ('', 'dist/', 'frontend/dist/', 'frontend-ui/dist/', 'dist\\'):
            separator = '\\' if '\\' in prefix else '/'
            files = {prefix + 'index.html': '<script src="/assets/app.js"></script>',
                     prefix + 'assets' + separator + 'app.js': 'valid'}
            installed = support.refresh_ui(self.root, self.archive(files))
            self.assertEqual((installed / 'assets/app.js').read_text(), 'valid')

    def test_unsafe_or_ambiguous_zip_layouts(self):
        for files in ({'../index.html': 'bad'}, {'C:/index.html': 'bad'},
                      {'dist\\..\\index.html': 'bad'}, {'/index.html': 'bad'},
                      {'a/index.html': 'one', 'b/index.html': 'two'}):
            with self.assertRaises(ValueError):
                support.refresh_ui(self.root, self.archive(files))

    def test_replacement_failure_restores_previous_ui(self):
        dist = self.root / 'frontend/dist'
        dist.mkdir(parents=True)
        (dist / 'index.html').write_text('old')
        archive = self.archive({'dist/index.html': 'new'})
        rename = Path.rename
        def fail_install(source, target):
            if source.name == 'dist' and source.parent.name == 'payload':
                raise OSError('simulated rename failure')
            return rename(source, target)
        with patch.object(Path, 'rename', fail_install):
            with self.assertRaisesRegex(OSError, 'simulated'):
                support.refresh_ui(self.root, archive)
        self.assertEqual((dist / 'index.html').read_text(), 'old')

    def fake_github(self, files):
        """An opener serving release assets; missing names answer 404."""
        import io
        import urllib.error
        requested = []
        def opener(request, timeout=None):
            url = request.full_url
            requested.append(url)
            name = url.rsplit('/', 1)[1]
            if name not in files:
                raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
            return io.BytesIO(files[name])
        return opener, requested

    def test_fetch_ui_waits_for_the_build_installs_it_and_skips_when_current(self):
        import hashlib
        version = 'ab' * 20
        data = self.archive({'dist/index.html': '<script src="/assets/a.js"></script>', 'dist/assets/a.js': 'a'}).read_bytes()
        files = {}
        opener, requested = self.fake_github(files)
        def git(app, *args):
            return version if args[0] == 'rev-parse' else 'https://github.com/owner/repo.git'
        def sleep(seconds):
            # The build lands while the notebook waits.
            files[f'frontend-ui-{version}.zip'] = data
            files[f'frontend-ui-{version}.zip.sha256'] = (hashlib.sha256(data).hexdigest() + '\n').encode()
        logs = []
        with patch.object(support, '_git', side_effect=git):
            dist = support.fetch_ui(self.root, opener=opener, sleep=sleep, log=logs.append)
            self.assertEqual((dist / 'assets/a.js').read_text(), 'a')
            self.assertEqual((dist / support.UI_MARKER).read_text().strip(), version)
            self.assertEqual(requested[0], f'https://github.com/owner/repo/releases/download/ui-latest/frontend-ui-{version}.zip')
            self.assertTrue(any('still building' in line for line in logs))
            count = len(requested)
            support.fetch_ui(self.root, opener=opener, log=logs.append)
            self.assertEqual(len(requested), count)  # already current: nothing downloaded

    def test_fetch_ui_rejects_corrupt_downloads_and_gives_up_after_waiting(self):
        version = 'cd' * 20
        (self.root / 'frontend/dist').mkdir(parents=True)
        (self.root / 'frontend/dist/index.html').write_text('old')
        git = lambda app, *args: version if args[0] == 'rev-parse' else 'git@github.com:owner/repo.git'
        corrupt, _ = self.fake_github({f'frontend-ui-{version}.zip': b'zip', f'frontend-ui-{version}.zip.sha256': b'0' * 64})
        missing, _ = self.fake_github({})
        ticks = iter(range(0, 10000, 100))
        with patch.object(support, '_git', side_effect=git):
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                support.fetch_ui(self.root, opener=corrupt, log=lambda line: None)
            with self.assertRaisesRegex(RuntimeError, 'no UI build'):
                support.fetch_ui(self.root, wait=300, opener=missing, sleep=lambda s: None, clock=lambda: next(ticks), log=lambda line: None)
        self.assertEqual((self.root / 'frontend/dist/index.html').read_text(), 'old')

    def setup_cell_scope(self):
        import shutil
        (self.root / '.git').mkdir()
        return dict(studio_support=support, APP=self.root, PORT=12345, UI_ZIP=self.root / 'missing.zip', UPDATE_SOURCE=True,
                    sys=sys, subprocess=subprocess, shutil=shutil, spec=Mock())

    def test_git_setup_pulls_then_downloads_the_matching_ui_before_setup(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, 'Already up to date.', '')
        def fetch(app):
            self.assertEqual(len(calls), 1)  # after the pull, before setup
            (self.root / 'frontend/dist/assets').mkdir(parents=True)
            return self.root / 'frontend/dist'
        scope = self.setup_cell_scope()
        import shutil
        with patch.object(support, 'assert_stopped'), patch.object(shutil, 'which', return_value=None), \
                patch.object(subprocess, 'run', side_effect=run), patch.object(support, 'fetch_ui', side_effect=fetch) as fetch_ui:
            exec(self.cell('studio-3'), scope)
        fetch_ui.assert_called_once()
        scope['spec'].loader.exec_module.assert_called_once_with(support)
        self.assertEqual(calls[0][-4:], ['pull', '--ff-only', 'origin', 'main'])
        self.assertIn('--skip-ui-build', calls[1])

    def test_pull_blocked_by_an_edited_notebook_explains_what_to_do(self):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, '', 'error: Your local changes to the following files would be overwritten by merge:\n\tJupyter Studio.ipynb\n')
        scope = self.setup_cell_scope()
        import shutil
        with patch.object(support, 'assert_stopped'), patch.object(shutil, 'which', return_value=None), \
                patch.object(subprocess, 'run', side_effect=run), patch.object(support, 'fetch_ui') as fetch_ui:
            with self.assertRaisesRegex(RuntimeError, 'Save Notebook As'):
                exec(self.cell('studio-3'), scope)
        fetch_ui.assert_not_called()

    def test_notebook_code_cells_parse_and_have_no_outputs(self):
        for cell in self.notebook['cells']:
            if cell['cell_type'] == 'code':
                ast.parse(''.join(cell['source']))
                self.assertEqual(cell['outputs'], [])
