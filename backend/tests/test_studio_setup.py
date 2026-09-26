"""Source setup refreshes stale UI; binary-only distributions retain their UI."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('studio_launcher', ROOT / 'studio.py')
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


class SetupTests(unittest.TestCase):
    def check_setup(self, source, built, skip=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'frontend/dist').mkdir(parents=True)
            if source:
                (root / 'frontend/package.json').write_text('{}')
            if built:
                (root / 'frontend/dist/index.html').write_text('old UI')
            venv = root / 'python.exe'
            venv.touch()
            with patch.object(studio, 'ROOT', root), patch.object(studio, 'VENV', venv), patch.object(studio.shutil, 'which', return_value='npm'), patch.object(studio.subprocess, 'run') as run:
                studio.setup(skip_ui_build=skip)
                return run.call_args_list

    def test_source_rebuilds_existing_ui(self):
        calls = self.check_setup(source=True, built=True)
        self.assertEqual(calls[1].args[0], ['npm', 'ci'])
        self.assertEqual(calls[2].args[0], ['npm', 'run', 'build'])
        self.assertEqual(calls[2].kwargs['env']['VITE_BACKEND_URL'], '')

    def test_release_needs_no_node(self):
        calls = self.check_setup(source=False, built=True)
        self.assertEqual(len(calls), 1)
        self.assertIn('pip', calls[0].args[0])

    def test_incomplete_distribution_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'both missing'):
            self.check_setup(source=False, built=False)

    def test_uploaded_build_without_node(self):
        calls = self.check_setup(source=True, built=True, skip=True)
        self.assertEqual(len(calls), 1)

    def test_skip_requires_uploaded_build(self):
        with self.assertRaises(RuntimeError):
            self.check_setup(source=True, built=False, skip=True)
