import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.providers.gallery_dl import GalleryDLProvider
from app.services.media import _new_temp_path


class DownloadIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def run_helper(self, script, cancel=False):
        instance = object.__new__(GalleryDLProvider)
        instance.site = "pawchive"
        original_spawn = asyncio.create_subprocess_exec
        processes = []
        started = asyncio.Event()
        with tempfile.TemporaryDirectory() as root:
            scratch = Path(root)

            async def spawn(*args, **kwargs):
                self.assertIn("--config-ignore", args)
                self.assertIn("--no-download", args)
                self.assertIn("--resolve-json", args)
                self.assertIn("-I", args)
                self.assertEqual(args[-2], "--")
                workdir = Path(kwargs["cwd"])
                self.assertEqual(workdir.parent, scratch)
                config = json.loads(Path(args[args.index("--config") + 1]).read_text())
                self.assertFalse(config["extractor"]["download"])
                self.assertEqual(config["extractor"]["base-directory"], str(workdir))
                self.assertEqual(Path(config["cache"]["file"]).parent, workdir)
                for key in ("TEMP", "TMP", "TMPDIR"):
                    self.assertEqual(kwargs["env"][key], str(workdir))
                process = await original_spawn(sys.executable, "-I", "-c", script, **kwargs)
                processes.append(process)
                started.set()
                return process

            with patch.object(instance, "_config", return_value={"extractor": {}}), \
                    patch("app.providers.gallery_dl.scratch_directory", return_value=scratch), \
                    patch("app.providers.gallery_dl.asyncio.create_subprocess_exec", side_effect=spawn):
                task = asyncio.create_task(instance._run_gallery_dl("https://example.test/artist", 1, 20))
                if cancel:
                    await asyncio.wait_for(started.wait(), 10)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                elif "sys.exit(1)" in script:
                    with self.assertRaises(RuntimeError):
                        await task
                else:
                    self.assertEqual(await task, [])
            self.assertIsNotNone(processes[0].returncode)
            self.assertEqual(list(scratch.iterdir()), [])

    async def test_relative_and_temp_helper_writes_cleaned_on_success_and_failure(self):
        script = (
            "import pathlib, tempfile, sys; "
            "pathlib.Path('stray.gif').write_bytes(b'GIF89a'); "
            "tempfile.NamedTemporaryFile(suffix='.gif', delete=False).close(); "
            "print('[]'); "
        )
        await self.run_helper(script)
        await self.run_helper(script + "sys.exit(1)")

    async def test_cancel_reaps_helper_before_cleaning_directory(self):
        await self.run_helper("import time; time.sleep(60)", cancel=True)

    def test_media_temp_uses_app_directory_not_environment(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("app.services.workspace.scratch_directory", return_value=Path(root)):
                path = Path(_new_temp_path(".gif"))
            self.assertEqual(path.parent, Path(root))
            self.assertTrue(path.exists())
            path.unlink()
