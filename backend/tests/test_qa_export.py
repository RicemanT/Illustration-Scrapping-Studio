import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import app.db as db
from app.services.qa import DatasetQAService


class DatasetQAExportTests(unittest.TestCase):
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
               VALUES ('Training Set', 'training-set', 'artist', 'artist', NULL, '{}', '{}', ?, ?, 1)""",
            (now, now),
        )
        folder = self.root / "images" / "training-set"
        folder.mkdir(parents=True)
        image_path = folder / "fixture.jpg"
        Image.new("RGB", (512, 512), "navy").save(image_path, "JPEG", quality=100)
        content = image_path.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        conn.execute(
            """INSERT INTO image (sha256, md5, width, height, format, file_size, path, added_at, folder_id)
               VALUES (?, ?, 512, 512, 'jpg', ?, 'training-set/fixture.jpg', ?, 1)""",
            (sha256, hashlib.md5(content).hexdigest(), len(content), now),
        )
        conn.execute("INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (1, 1, ?)", (now,))
        conn.execute(
            """INSERT INTO image_source
               (image_id, provider, remote_id, remote_url, source_url, fetched_at, metadata, version, metadata_version, is_primary)
               VALUES (1, 'danbooru', '42', 'https://example/posts/42', 'https://example/posts/42', ?, ?, 1, 1, 1)""",
            (now, json.dumps({"id": 42, "score": 7})),
        )
        source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for tag in ("blue_hair", "solo"):
            conn.execute("INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, 'general', ?)", (source_id, tag))
        conn.commit()
        conn.close()
        image_path.with_suffix(".txt").write_text("blue hair, solo", encoding="utf-8")
        self.image_path = image_path
        self.service = DatasetQAService(self.root)

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def test_clean_collection_validates_and_exports_existing_pair_with_manifest(self):
        validation = self.service.validate_collection(1)
        self.assertTrue(validation["ready"])
        self.assertEqual(validation["valid_image_count"], 1)

        result = self.service.create_export(1, "copy")
        self.assertEqual(result["status"], "completed")
        output = Path(result["output_path"])
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        item = manifest["items"][0]
        self.assertEqual((output / item["filename"]).read_bytes(), self.image_path.read_bytes())
        self.assertEqual((output / item["sidecar_filename"]).read_text(encoding="utf-8"), "blue hair, solo")
        self.assertEqual(item["ground_truth_tags"], ["blue hair", "solo"])
        self.assertEqual(item["sources"][0]["metadata"]["score"], 7)
        self.assertEqual(manifest["caption_policy"], "existing_curated_sidecars_only")
        self.assertNotIn("caption", item)
        self.assertEqual(self.service.get_export(result["id"])["status"], "completed")

    def test_missing_sidecar_and_corrupt_image_block_export_without_modifying_library(self):
        self.image_path.with_suffix(".txt").unlink()
        original = self.image_path.read_bytes()
        validation = self.service.validate_collection(1)
        self.assertIn("missing_sidecar", validation["summary"]["by_code"])
        with self.assertRaises(ValueError):
            self.service.create_export(1, "copy")
        self.assertEqual(self.image_path.read_bytes(), original)
        self.assertFalse((self.root / "exports").exists())

        self.image_path.write_bytes(b"not an image")
        validation = self.service.validate_collection(1)
        self.assertIn("corrupt_image", validation["summary"]["by_code"])
        self.assertIn("hash_mismatch", validation["summary"]["by_code"])

    def test_stale_sidecar_and_empty_tags_are_reported(self):
        self.image_path.with_suffix(".txt").write_text("stale tags", encoding="utf-8")
        validation = self.service.validate_collection(1)
        self.assertIn("sidecar_mismatch", validation["summary"]["by_code"])

        conn = db.get_connection()
        conn.execute("DELETE FROM image_tag")
        conn.commit()
        conn.close()
        self.image_path.with_suffix(".txt").write_text("", encoding="utf-8")
        validation = self.service.validate_collection(1)
        self.assertIn("empty_ground_truth_tags", validation["summary"]["by_code"])

    def test_legacy_format_and_oversized_image_block_export(self):
        conn = db.get_connection()
        conn.execute("UPDATE image SET format = 'png', width = 2001 WHERE id = 1")
        conn.commit()
        conn.close()
        validation = self.service.validate_collection(1)
        self.assertIn("noncanonical_training_format", validation["summary"]["by_code"])
        self.assertIn("oversized_image", validation["summary"]["by_code"])
        self.assertFalse(validation["ready"])

    def test_folder_minimum_dimensions_block_existing_small_frames(self):
        conn = db.get_connection()
        conn.execute(
            "UPDATE collection SET filters = ? WHERE id = 1",
            (json.dumps({"min_width": 768, "min_height": 768, "min_aspect_ratio": 0.33, "max_aspect_ratio": 3.0}),),
        )
        conn.commit()
        conn.close()
        validation = self.service.validate_collection(1)
        self.assertIn("undersized_image", validation["summary"]["by_code"])
        self.assertFalse(validation["ready"])

    def test_path_collisions_and_original_preview_fallback_are_visible(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute("UPDATE image SET derived_from_preview = 1, derived_media_source = 'provider_preview', original_media_format = 'webm' WHERE id = 1")
        conn.execute(
            """INSERT INTO image (sha256, md5, width, height, format, file_size, path, added_at, folder_id)
               VALUES (?, ?, 512, 512, 'jpg', ?, 'TRAINING-SET/FIXTURE.JPG', ?, 1)""",
            ("f" * 64, "e" * 32, self.image_path.stat().st_size, now),
        )
        second_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (1, ?, ?)", (second_id, now))
        conn.commit()
        conn.close()

        validation = self.service.validate_collection(1)
        self.assertIn("path_collision", validation["summary"]["by_code"])
        self.assertIn("sidecar_collision", validation["summary"]["by_code"])
        self.assertIn("unsupported_original_fallback", validation["summary"]["by_code"])
        self.assertFalse(validation["ready"])


if __name__ == "__main__":
    unittest.main()
