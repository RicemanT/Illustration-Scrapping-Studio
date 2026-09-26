import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import app.db as db
from app.models import FolderCreate, RemotePost, SyncResult
from app.routes import sync as sync_routes
from app.services.collections import CollectionService
from app.services.sync import SyncService
from app.services.sync_jobs import create_job, get_job, get_schedule, list_jobs, save_job, update_schedule


def remote_post(remote_id: int) -> RemotePost:
    return RemotePost(
        provider="danbooru", remote_id=str(remote_id),
        remote_url=f"https://example.test/posts/{remote_id}",
        image_url=f"https://example.test/{remote_id}.jpg",
        width=1024, height=1024, format="jpg", tags={"artist": ["artist"]},
        created_at=datetime.now(timezone.utc).isoformat(), raw_metadata={"id": remote_id},
    )


class SyncJobPhaseFiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_post_ingests_three_frames_and_repeat_skips_download(self):
        folder = CollectionService().create_collection(
            FolderCreate(name="Three Frames", query="three frames", sources=["pawchive"])
        )
        post = remote_post(99).model_copy(update={
            "provider": "pawchive", "format": "mp4", "image_url": "https://example.test/99.mp4",
        })
        items = []
        for number, color in enumerate(("red", "green", "blue"), 1):
            path = self.root / f"frame-{number}.png"
            Image.new("RGB", (640, 640), color).save(path)
            frame_post = post.model_copy(update={
                "remote_id": f"99:frame:{number}", "format": "png", "md5": None,
                "raw_metadata": {"_artist_collection_import": {"source_remote_id": "99", "frame_number": number, "frame_count": 3}},
            })
            items.append((str(path), frame_post, "original_frame"))
        service = SyncService(self.root)
        with patch("app.services.sync.download_importable_images", new=AsyncMock(return_value=items)) as download:
            result = await service._process_post_once(object(), post, folder.id, {}, {"webp"})
        self.assertEqual(result["status"], "new")
        self.assertEqual(result["new_images"], 3)
        self.assertEqual(download.await_count, 1)
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image WHERE folder_id = ?", (folder.id,)).fetchone()[0], 3)
        conn.close()
        with patch("app.services.sync.download_importable_images", new=AsyncMock(side_effect=AssertionError("redownloaded"))) as download:
            repeat = await service._process_post_once(object(), post, folder.id, {}, {"webp"})
        self.assertEqual(repeat["status"], "skipped")
        download.assert_not_awaited()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"
        db.init_db()
        sync_routes._jobs.clear()
        sync_routes._tasks.clear()

    async def asyncTearDown(self):
        for task in list(sync_routes._tasks.values()):
            task.cancel()
        if sync_routes._tasks:
            await asyncio.gather(*sync_routes._tasks.values(), return_exceptions=True)
        sync_routes._jobs.clear()
        sync_routes._tasks.clear()
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    async def test_job_and_schedule_state_are_durable(self):
        job = sync_routes._new_job("all", "manual", {"limit": 20, "sort": "latest"})
        create_job(job)
        job.update(status="completed", result={"new_images": 3}, finished_at=datetime.now(timezone.utc).isoformat())
        save_job(job)

        stored = get_job(job["job_id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["result"]["new_images"], 3)
        self.assertEqual(list_jobs(1)[0]["job_id"], job["job_id"])

        schedule = update_schedule(True, 60, 100, "latest")
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["interval_minutes"], 60)
        self.assertEqual(schedule["limit_per_source"], 100)
        self.assertEqual(get_schedule()["sort"], "latest")

    async def test_sync_all_is_backgrounded_and_isolates_source_failures(self):
        first = CollectionService().create_collection(FolderCreate(name="First", query="first", sources=["danbooru"]))
        CollectionService().create_collection(FolderCreate(name="Second", query="second", sources=["danbooru"]))
        received_options = []

        async def fake_sync(folder_id, provider, *args, **kwargs):
            received_options.append({
                "folder_id": folder_id,
                "limit": args[0],
                "sort": kwargs.get("sort"),
                "date_from": kwargs.get("date_from"),
                "date_to": kwargs.get("date_to"),
            })
            if folder_id == first.id:
                raise RuntimeError("temporary provider failure")
            kwargs["progress"]({
                "phase": "download", "total": 2, "completed": 1,
                "message": "Downloaded image for 22",
                "current_file": {
                    "stage": "complete", "remote_id": "22", "format": "jpg",
                    "bytes_downloaded": 1048576, "bytes_total": 1048576,
                    "speed_bps": 524288, "elapsed_seconds": 2.0,
                },
                "active_files": [],
            })
            return SyncResult(folder_id=folder_id, provider=provider, new_images=2, skipped=1, errors=0, duration_seconds=0.1)

        with patch.object(sync_routes.sync_service, "sync_collection", side_effect=fake_sync):
            job = await sync_routes.sync_all(
                limit=37, sort="oldest", date_from="2025-01-01", date_to="2025-03-31"
            )
            await sync_routes._tasks[job["job_id"]]

        stored = get_job(job["job_id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["result"]["new_images"], 2)
        self.assertEqual(stored["result"]["errors"], 1)
        self.assertEqual(len(stored["result"]["sources"]), 2)
        self.assertTrue(all(options["limit"] == 37 for options in received_options))
        self.assertTrue(all(options["sort"] == "oldest" for options in received_options))
        self.assertTrue(all(options["date_from"] == "2025-01-01" for options in received_options))
        self.assertTrue(all(options["date_to"] == "2025-03-31" for options in received_options))
        transfer_logs = [log for log in stored["logs"] if log.get("current_file")]
        self.assertEqual(transfer_logs[-1]["current_file"]["speed_bps"], 524288)

    async def test_history_overlays_live_transfer_progress(self):
        job = sync_routes._new_job("all", "manual", {"limit": 20, "sort": "latest"})
        create_job(job)
        job.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
        job["progress"] = {
            "phase": "sync_all", "current_folder_id": 7,
            "current_file": {"remote_id": "99", "speed_bps": 3145728},
        }
        sync_routes._jobs[job["job_id"]] = job

        response = await sync_routes.sync_history(limit=10)

        current = next(item for item in response["items"] if item["job_id"] == job["job_id"])
        self.assertEqual(current["progress"]["current_folder_id"], 7)
        self.assertEqual(current["progress"]["current_file"]["speed_bps"], 3145728)

    async def test_incremental_watermark_and_backfill_advance_only_after_success(self):
        folder = CollectionService().create_collection(FolderCreate(name="Cursor", query="cursor artist", sources=["danbooru"]))
        service = SyncService(self.root)

        class FirstProvider:
            async def search(self, query, cursor, limit, **kwargs):
                return [remote_post(105), remote_post(104)], "104"

            async def close(self):
                return None

        async def successful_post(*args, **kwargs):
            return {"status": "new", "message": "ok", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=FirstProvider()), patch.object(service, "_process_post", side_effect=successful_post):
            await service.sync_collection(folder.id, "danbooru", limit=2)

        conn = db.get_connection()
        cursor = conn.execute("SELECT last_cursor, backfill_cursor FROM collection_source WHERE collection_id = ?", (folder.id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(cursor), ("105", "104"))

        calls = []

        class NextProvider:
            async def search(self, query, cursor, limit, sort="latest", **kwargs):
                calls.append((query, cursor, limit, sort))
                if "id:>105" in query:
                    return [remote_post(106), remote_post(107)], None
                return [remote_post(103), remote_post(102)], "102"

            async def close(self):
                return None

        with patch("app.services.sync.create_provider", return_value=NextProvider()), patch.object(service, "_process_post", side_effect=successful_post):
            await service.sync_collection(folder.id, "danbooru", limit=4)

        self.assertEqual(calls[0][3], "oldest")
        self.assertEqual(calls[1][1], "104")
        conn = db.get_connection()
        cursor = conn.execute("SELECT last_cursor, backfill_cursor FROM collection_source WHERE collection_id = ?", (folder.id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(cursor), ("107", "102"))

        async def failed_post(*args, **kwargs):
            return {"status": "error", "message": "failed", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=NextProvider()), patch.object(service, "_process_post", side_effect=failed_post):
            await service.sync_collection(folder.id, "danbooru", limit=4)
        conn = db.get_connection()
        unchanged = conn.execute("SELECT last_cursor, backfill_cursor FROM collection_source WHERE collection_id = ?", (folder.id,)).fetchone()
        conn.close()
        self.assertEqual(tuple(unchanged), ("107", "102"))

    async def test_transient_search_failure_uses_bounded_backoff(self):
        folder = CollectionService().create_collection(FolderCreate(name="Retry", query="retry artist", sources=["danbooru"]))
        service = SyncService(self.root)

        class RetryProvider:
            def __init__(self):
                self.attempts = 0

            async def search(self, *args, **kwargs):
                self.attempts += 1
                if self.attempts < 3:
                    raise httpx.ConnectError("offline")
                return [remote_post(200)], "200"

            async def close(self):
                return None

        provider = RetryProvider()
        async def successful_post(*args, **kwargs):
            return {"status": "new", "message": "ok", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=provider), patch.object(
            service, "_process_post", side_effect=successful_post
        ), patch("app.services.sync.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await service.sync_collection(folder.id, "danbooru", limit=1)

        self.assertEqual(provider.attempts, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])
        self.assertEqual(result.new_images, 1)

    async def test_provider_discovery_progress_is_forwarded_during_search(self):
        folder = CollectionService().create_collection(FolderCreate(
            name="Discovery", query="discovery artist", sources=["danbooru"]
        ))
        service = SyncService(self.root)
        updates = []

        class ReportingProvider:
            search_progress = None

            async def search(self, *_args, **_kwargs):
                self.search_progress({
                    "phase": "discover", "total": 1, "completed": 1,
                    "message": "Matched project 1/1",
                })
                return [remote_post(300)], "300"

            async def close(self):
                return None

        async def successful_post(*_args, **_kwargs):
            return {"status": "new", "message": "ok", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=ReportingProvider()), patch.object(
            service, "_process_post", side_effect=successful_post
        ):
            await service.sync_collection(folder.id, "danbooru", limit=1, progress=updates.append)

        self.assertTrue(any(update.get("message") == "Matched project 1/1" for update in updates))
        self.assertTrue(any(update.get("message") == "Searching danbooru for matching posts..." for update in updates))

    async def test_ordered_profile_source_checks_head_then_continues_backfill(self):
        folder = CollectionService().create_collection(FolderCreate(name="Profile", query="display name", sources=["pixiv"]))
        conn = db.get_connection()
        conn.execute("UPDATE collection_source SET query_override = ? WHERE collection_id = ?", ("12345", folder.id))
        conn.commit()
        conn.close()
        service = SyncService(self.root)
        calls = []

        class ProfileProvider:
            ordered_feed = True

            async def search(self, query, cursor, limit, **kwargs):
                calls.append((query, cursor, limit))
                start = int(cursor or 0)
                return [remote_post(start + index + 1) for index in range(limit)], str(start + limit)

            async def close(self):
                return None

        async def successful_post(*args, **kwargs):
            return {"status": "new", "message": "ok", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=ProfileProvider()), patch.object(service, "_process_post", side_effect=successful_post):
            await service.sync_collection(folder.id, "pixiv", limit=6)
            await service.sync_collection(folder.id, "pixiv", limit=6)

        self.assertEqual(calls[0], ("artist:12345", None, 6))
        self.assertEqual(calls[1], ("artist:12345", None, 2))
        self.assertEqual(calls[2], ("artist:12345", "6", 4))
        conn = db.get_connection()
        state = conn.execute("SELECT backfill_cursor FROM collection_source WHERE collection_id = ?", (folder.id,)).fetchone()
        conn.close()
        self.assertEqual(state[0], "10")

    async def test_exhausted_profile_backfill_refills_requested_latest_page(self):
        folder = CollectionService().create_collection(
            FolderCreate(name="Short Pawchive Feed", query="pawchive artist", sources=["pawchive"])
        )
        conn = db.get_connection()
        conn.execute(
            "UPDATE collection_source SET backfill_cursor = '20' WHERE collection_id = ? AND provider = 'pawchive'",
            (folder.id,),
        )
        conn.commit()
        conn.close()
        calls = []

        class ShortFeedProvider:
            ordered_feed = True

            async def search(self, query, cursor, limit, **kwargs):
                calls.append((cursor, limit))
                if cursor:
                    return [], None
                return [remote_post(number) for number in range(15, 15 - min(limit, 15), -1)], None

            async def close(self):
                return None

        service = SyncService(self.root)

        async def successful_post(*args, **kwargs):
            return {"status": "new", "message": "ok", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=ShortFeedProvider()), patch.object(
            service, "_process_post", side_effect=successful_post
        ):
            result = await service.sync_collection(folder.id, "pawchive", limit=20)

        self.assertEqual(calls, [(None, 6), ("20", 14), (None, 20)])
        self.assertEqual(result.new_images, 15)
        conn = db.get_connection()
        state = conn.execute(
            "SELECT backfill_cursor FROM collection_source WHERE collection_id = ? AND provider = 'pawchive'",
            (folder.id,),
        ).fetchone()
        conn.close()
        self.assertIsNone(state[0])

    async def test_ordered_profile_overlap_fetches_more_distinct_items(self):
        folder = CollectionService().create_collection(
            FolderCreate(name="Moving Feed", query="moving artist", sources=["pawchive"])
        )
        conn = db.get_connection()
        conn.execute(
            "UPDATE collection_source SET backfill_cursor = '1' WHERE collection_id = ? AND provider = 'pawchive'",
            (folder.id,),
        )
        conn.commit()
        conn.close()
        calls = []

        class MovingFeedProvider:
            ordered_feed = True

            async def search(self, query, cursor, limit, **kwargs):
                calls.append((cursor, limit))
                if cursor is None:
                    return [remote_post(10), remote_post(9)], "2"
                if cursor == "1":
                    return [remote_post(number) for number in (9, 8, 7, 6)], "5"
                return [remote_post(5)], "6"

            async def close(self):
                return None

        processed = []
        service = SyncService(self.root)

        async def successful_post(provider, post, *args, **kwargs):
            processed.append(post.remote_id)
            return {"status": "new", "message": "ok", "transfer": None}

        with patch("app.services.sync.create_provider", return_value=MovingFeedProvider()), patch.object(
            service, "_process_post", side_effect=successful_post
        ):
            result = await service.sync_collection(folder.id, "pawchive", limit=6)

        self.assertEqual(calls, [(None, 2), ("1", 4), ("5", 1)])
        self.assertEqual(result.new_images, 6)
        self.assertEqual(processed, ["10", "9", "8", "7", "6", "5"])


if __name__ == "__main__":
    unittest.main()
