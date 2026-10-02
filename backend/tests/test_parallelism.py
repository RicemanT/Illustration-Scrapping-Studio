import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app.db as db
from app.routes import imports as import_routes
from app.models import FolderCreate, RemotePost
from app.services.collections import CollectionService
from app.services.settings import get_parallel_workers, set_parallel_workers
from app.services.sync import SyncService


def make_post(remote_id: int) -> RemotePost:
    return RemotePost(
        provider="danbooru",
        remote_id=str(remote_id),
        remote_url=f"https://example.test/posts/{remote_id}",
        image_url=f"https://example.test/files/{remote_id}.jpg",
        width=1024,
        height=1024,
        format="jpg",
        tags={"artist": ["example_artist"]},
        created_at=datetime.now(timezone.utc).isoformat(),
        raw_metadata={"id": remote_id},
    )


class ParallelSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"
        db.init_db()
        self.folder = CollectionService().create_collection(
            FolderCreate(name="Parallel Artist", query="parallel artist", sources=["danbooru"])
        )

    async def asyncTearDown(self):
        import_routes._jobs.clear()
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    async def test_worker_setting_is_persisted_and_bounded(self):
        self.assertEqual(get_parallel_workers(), 2)
        self.assertEqual(set_parallel_workers(4), 4)
        self.assertEqual(get_parallel_workers(), 4)
        self.assertEqual(set_parallel_workers(99), 99)
        self.assertEqual(set_parallel_workers(-1), 1)

    async def test_sync_processes_at_most_configured_posts_in_parallel(self):
        set_parallel_workers(2)
        posts = [make_post(value) for value in range(1, 7)]
        active = 0
        peak = 0
        progress_updates = []

        class FakeProvider:
            async def search(self, *args, **kwargs):
                return posts, None

            async def close(self):
                return None

        async def fake_process(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return {
                "status": "new",
                "message": "processed",
                "transfer": {"stage": "complete", "percent": 100},
            }

        service = SyncService(self.root)
        with patch("app.services.sync.create_provider", return_value=FakeProvider()), patch.object(
            service, "_process_post", side_effect=fake_process
        ):
            result = await service.sync_collection(
                self.folder.id,
                "danbooru",
                limit=len(posts),
                progress=progress_updates.append,
            )

        self.assertEqual(peak, 2)
        self.assertEqual(result.new_images, len(posts))
        self.assertEqual(result.errors, 0)
        self.assertTrue(any(update.get("workers") == 2 for update in progress_updates))

    async def test_import_batch_uses_the_same_worker_limit(self):
        set_parallel_workers(2)
        remote_ids = [str(value) for value in range(1, 7)]
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO import_batch (collection_id, provider, status, created_at, job_id) VALUES (?, 'danbooru', 'queued', ?, 'parallel-import')",
            (self.folder.id, now),
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.executemany(
            "INSERT INTO import_item (batch_id, remote_id) VALUES (?, ?)",
            [(batch_id, remote_id) for remote_id in remote_ids],
        )
        conn.commit()
        conn.close()
        import_routes._jobs["parallel-import"] = import_routes._new_job(
            "parallel-import", batch_id, len(remote_ids)
        )
        active = 0
        peak = 0

        class FakeProvider:
            async def get_post(self, remote_id):
                return make_post(int(remote_id))

            async def close(self):
                return None

        async def fake_download(provider, post, progress=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if progress:
                progress({"stage": "downloading", "remote_id": post.remote_id, "format": post.format})
            await asyncio.sleep(0.03)
            target = self.root / f"{post.remote_id}.jpg"
            target.write_bytes(b"test")
            active -= 1
            return str(target), post, None

        def fake_ingest(path, post, collection_id, **kwargs):
            Path(path).unlink(missing_ok=True)
            return None, True

        with patch("app.routes.imports.create_provider", return_value=FakeProvider()), patch(
            "app.services.post_ingest.download_importable_image", side_effect=fake_download
        ), patch("app.services.post_ingest.ensure_image_meets_folder_quality"), patch.object(
            import_routes.ImageService, "ingest_image", side_effect=fake_ingest
        ):
            await import_routes._run_import(
                "parallel-import", batch_id, self.folder.id, "danbooru", remote_ids
            )

        job = import_routes._jobs["parallel-import"]
        self.assertEqual(peak, 2)
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["progress"]["downloaded"], len(remote_ids))
        self.assertEqual(job["progress"]["workers"], 2)

    async def test_selected_video_import_counts_three_images_from_one_post(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO import_batch (collection_id, provider, status, created_at, job_id) VALUES (?, 'danbooru', 'queued', ?, 'three-frame-import')",
            (self.folder.id, now),
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO import_item (batch_id, remote_id) VALUES (?, '7')", (batch_id,))
        conn.commit()
        conn.close()
        import_routes._jobs["three-frame-import"] = import_routes._new_job("three-frame-import", batch_id, 1)
        post = make_post(7).model_copy(update={"format": "mp4", "image_url": "https://example.test/7.mp4"})
        items = []
        for number in range(1, 4):
            path = self.root / f"import-frame-{number}.png"
            path.write_bytes(b"fixture")
            frame_post = post.model_copy(update={
                "remote_id": f"7:frame:{number}", "format": "png", "md5": None,
                "raw_metadata": {"_artist_collection_import": {"frame_count": 3}},
            })
            items.append((str(path), frame_post, "original_frame"))

        class FakeProvider:
            async def get_post(self, remote_id):
                return post

            async def close(self):
                return None

        def fake_ingest(path, post, collection_id, **kwargs):
            Path(path).unlink(missing_ok=True)
            return None, True

        with patch("app.routes.imports.create_provider", return_value=FakeProvider()), patch(
            "app.services.post_ingest.download_importable_images", new=AsyncMock(return_value=items)
        ) as download, patch("app.services.post_ingest.ensure_image_meets_folder_quality"), patch.object(
            import_routes.ImageService, "ingest_image", side_effect=fake_ingest
        ):
            await import_routes._run_import("three-frame-import", batch_id, self.folder.id, "danbooru", ["7"])
        job = import_routes._jobs["three-frame-import"]
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["progress"]["downloaded"], 3)
        self.assertEqual(job["progress"]["completed"], 1)
        download.assert_awaited_once()

    async def test_repeat_sync_skips_media_transfer_by_exact_source_id(self):
        service = SyncService(self.root)
        post = make_post(42)
        download = AsyncMock(side_effect=AssertionError("repeat source was downloaded"))

        with patch.object(
            service.image_service, "check_existing_by_source", return_value=77
        ), patch.object(service.image_service, "_add_source_record") as refresh_source, patch(
            "app.services.sync.download_importable_image", download
        ):
            result = await service._process_post_once(
                object(), post, self.folder.id, {}, {"jpg", "webp"}
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["image_id"], 77)
        self.assertIn("skipped media download", result["message"])
        download.assert_not_awaited()
        refresh_source.assert_called_once_with(77, post)

    async def test_repeat_selected_import_also_skips_media_transfer(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO import_batch (collection_id, provider, status, created_at, job_id) VALUES (?, 'danbooru', 'queued', ?, 'repeat-import')",
            (self.folder.id, now),
        )
        batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO import_item (batch_id, remote_id) VALUES (?, '42')",
            (batch_id,),
        )
        conn.execute(
            """INSERT INTO image
               (id, sha256, width, height, format, file_size, path, added_at, folder_id)
               VALUES (88, 'repeat-source', 1024, 1024, 'webp', 1, 'parallel-artist/repeat.webp', ?, ?)""",
            (now, self.folder.id),
        )
        conn.commit()
        conn.close()
        import_routes._jobs["repeat-import"] = import_routes._new_job(
            "repeat-import", batch_id, 1
        )

        class FakeProvider:
            async def get_post(self, remote_id):
                return make_post(int(remote_id))

            async def close(self):
                return None

        download = AsyncMock(side_effect=AssertionError("repeat source was downloaded"))
        with patch("app.routes.imports.create_provider", return_value=FakeProvider()), patch(
            "app.services.post_ingest.download_importable_image", download
        ), patch.object(
            import_routes.ImageService, "check_existing_by_source", return_value=88
        ), patch.object(import_routes.ImageService, "_add_source_record") as refresh_source:
            await import_routes._run_import(
                "repeat-import", batch_id, self.folder.id, "danbooru", ["42"]
            )

        job = import_routes._jobs["repeat-import"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"]["skipped"], 1)
        download.assert_not_awaited()
        refresh_source.assert_called_once()


if __name__ == "__main__":
    unittest.main()
