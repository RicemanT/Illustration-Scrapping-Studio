import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.models import FolderCreate
from app.routes import sync as sync_routes
from app.services.collections import CollectionService


class SyncCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / "index.db"
        db.init_db()
        self.folder = CollectionService().create_collection(FolderCreate(name="Cancel", query="cancel", sources=["danbooru"]))
        sync_routes._jobs.clear()
        sync_routes._tasks.clear()

    async def asyncTearDown(self):
        tasks = list(sync_routes._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        sync_routes._jobs.clear()
        sync_routes._tasks.clear()
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    async def test_active_sync_can_be_canceled_and_runs_cleanup(self):
        started = asyncio.Event()
        cleaned_up = asyncio.Event()

        async def blocking_sync(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned_up.set()

        with patch.object(sync_routes.sync_service, "sync_collection", side_effect=blocking_sync):
            job = await sync_routes.sync_folder(self.folder.id, "danbooru", limit=10)
            job_id = job["job_id"]
            await asyncio.wait_for(started.wait(), timeout=1)
            await sync_routes.cancel_sync_job(job_id)
            await asyncio.wait_for(cleaned_up.wait(), timeout=1)
            await asyncio.sleep(0)

        self.assertEqual(sync_routes._jobs[job_id]["status"], "canceled")
        self.assertEqual(sync_routes._jobs[job_id]["progress"]["phase"], "canceled")
        self.assertTrue(sync_routes._jobs[job_id]["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
