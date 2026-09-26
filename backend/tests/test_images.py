import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import app.db as db
from app.models import RemotePost
from app.services.images import ImageService


class ImageIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"
        db.init_db()
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            """INSERT INTO collection
               (name, slug, type, query, artist_tag_template, caption_template, filters, created_at, updated_at, enabled)
               VALUES ('Artist', 'artist', 'artist', 'artist', 'Drawn by {artist}', '{}', '{}', ?, ?, 1)""",
            (now, now),
        )
        conn.commit()
        conn.close()
        self.service = ImageService(self.root)

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def _post(self, provider: str, remote_id: str, raw: dict) -> RemotePost:
        return RemotePost(
            provider=provider,
            remote_id=remote_id,
            remote_url=f"https://{provider}.example/posts/{remote_id}",
            image_url=f"https://{provider}.example/files/{remote_id}.png",
            preview_url=f"https://{provider}.example/previews/{remote_id}.jpg",
            width=64,
            height=48,
            format="png",
            md5=None,
            tags={"artist": ["test_artist"], "general": ["blue_hair"]},
            created_at="2026-09-11T00:00:00Z",
            raw_metadata=raw,
        )

    def _image_file(self, name: str) -> Path:
        path = self.root / name
        Image.new("RGB", (64, 48), "navy").save(path)
        return path

    def test_exact_reimport_reuses_file_and_keeps_versioned_provenance(self):
        first_post = self._post("danbooru", "10", {"id": 10, "score": 1})
        first_id, is_new = self.service.ingest_image(
            str(self._image_file("first.png")), first_post, 1
        )
        self.assertTrue(is_new)

        second_post = self._post("e621", "20", {"id": 20, "score": 2})
        second_path = self._image_file("second.png")
        second_id, is_new = self.service.ingest_image(
            str(second_path), second_post, 1
        )
        self.assertFalse(is_new)
        self.assertEqual(second_id, first_id)
        self.assertFalse(second_path.exists())

        changed_first = self._post("danbooru", "10", {"id": 10, "score": 3})
        self.service._add_source_record(first_id, changed_first)

        image = self.service.get_image_by_id(first_id)
        image_path = self.root / "images" / image["path"]
        locations = image["storage_locations"]
        self.assertEqual(locations["files"]["image"]["path"], str(image_path.resolve()))
        self.assertTrue(locations["files"]["sidecar"]["exists"])
        self.assertEqual(locations["database"]["path"], str(db.DB_PATH.resolve()))
        self.assertTrue(image_path.exists())
        self.assertEqual(image["format"], "webp")
        self.assertEqual(image_path.suffix, ".webp")
        self.assertEqual(image_path.with_suffix(".txt").read_text(encoding="utf-8"), "Drawn by artist, blue hair")
        self.assertEqual(image["ground_truth_tags"], ["Drawn by artist", "blue hair"])

        conn = db.get_connection()
        sources = [dict(row) for row in conn.execute(
            """SELECT provider, remote_id, remote_url, source_url, metadata,
                      version, metadata_version, is_primary
               FROM image_source WHERE image_id = ? ORDER BY id""",
            (first_id,),
        )]
        conn.close()
        self.assertEqual(len(sources), 3)
        self.assertEqual([row["version"] for row in sources if row["provider"] == "danbooru"], [1, 2])
        self.assertTrue(all(row["metadata_version"] == row["version"] for row in sources))
        self.assertTrue(all(row["source_url"] == row["remote_url"] for row in sources))
        self.assertEqual([row["is_primary"] for row in sources], [1, 0, 1])
        self.assertEqual(json.loads(sources[-1]["metadata"])["score"], 3)

    def test_source_lookup_is_scoped_to_the_owning_folder(self):
        post = self._post("pixiv", "12345:0", {"id": 12345, "page": 0})
        image_id, _ = self.service.ingest_image(
            str(self._image_file("source.png")), post, 1
        )

        self.assertEqual(
            self.service.check_existing_by_source("pixiv", "12345:0", 1),
            image_id,
        )
        self.assertIsNone(
            self.service.check_existing_by_source("pixiv", "12345:0", 999)
        )
        self.assertIsNone(
            self.service.check_existing_by_source("twitter", "12345:0", 1)
        )

    def test_batched_frame_lookup_latest_version_and_folder_isolation(self):
        from unittest.mock import patch
        post = self._post("pixiv", "motion:frame:1", {"_artist_collection_import": {"frame_count": 2}})
        image_id, _ = self.service.ingest_image(str(self._image_file("frame.png")), post, 1)
        post.raw_metadata = {"_artist_collection_import": {"frame_count": 3}}
        self.service._add_source_record(image_id, post)
        with patch("app.services.images.get_connection", wraps=db.get_connection) as connect:
            self.assertEqual(self.service.existing_frames("pixiv", "motion", 1), ([image_id, None, None], 3))
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.service.existing_frames("pixiv", "motion", 999), ([None, None, None], 0))
        self.assertEqual(self.service.existing_frames("twitter", "motion", 1), ([None, None, None], 0))

    def test_identical_file_is_stored_as_an_independent_pair_per_folder(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            """INSERT INTO collection
               (name, slug, type, query, caption_template, filters, created_at, updated_at, enabled)
               VALUES ('Second Artist', 'second-artist', 'artist', 'second', '{}', '{}', ?, ?, 1)""",
            (now, now),
        )
        second_folder_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        post = self._post("danbooru", "same-post", {"id": "same-post"})
        first_id, first_new = self.service.ingest_image(str(self._image_file("folder-one.png")), post, 1)
        second_id, second_new = self.service.ingest_image(str(self._image_file("folder-two.png")), post, second_folder_id)

        self.assertTrue(first_new)
        self.assertTrue(second_new)
        self.assertNotEqual(first_id, second_id)
        first = self.service.get_image_by_id(first_id)
        second = self.service.get_image_by_id(second_id)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertTrue((self.root / "images" / first["path"]).is_file())
        self.assertTrue((self.root / "images" / second["path"]).is_file())
        self.assertTrue((self.root / "images" / first["path"]).with_suffix(".txt").is_file())
        self.assertTrue((self.root / "images" / second["path"]).with_suffix(".txt").is_file())
        conn = db.get_connection()
        owners = [row[0] for row in conn.execute("SELECT folder_id FROM image WHERE id IN (?, ?) ORDER BY id", (first_id, second_id))]
        conn.close()
        self.assertEqual(owners, [1, second_folder_id])


if __name__ == "__main__":
    unittest.main()
