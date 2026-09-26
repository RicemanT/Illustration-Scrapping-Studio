import json
import tempfile
import unittest
from pathlib import Path
from app.services.storage_locations import image_storage_locations


class StorageLocationsTests(unittest.TestCase):
    def test_active_missing_external_database_and_trash_pattern(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            library = root / "library"
            full = library / "images" / "artist" / "abc.webp"
            full.parent.mkdir(parents=True)
            full.touch()
            database = root / "separate.db"
            database.touch()
            image = {"id": 4, "folder_id": 2, "path": "artist/abc.webp", "thumb_path": "artist/abc_thumb.jpg", "sources": [{"id": 9}]}
            result = image_storage_locations(image, library, database)
            self.assertEqual(result["files"]["image"]["path"], str(full.resolve()))
            self.assertTrue(result["files"]["image"]["exists"])
            self.assertFalse(result["files"]["thumbnail"]["exists"])
            self.assertEqual(result["database"]["path"], str(database.resolve()))
            self.assertTrue(result["trash"]["is_template"])
            self.assertIn("{deletion-token}", result["trash"]["files"]["sidecar"]["path"])
            self.assertFalse((library / ".trash").exists())
            self.assertEqual(result["metadata_records"]["source_ids"], [9])

    def test_deleted_image_uses_actual_manifest_and_reports_unsafe_path(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            trash = root / ".trash" / "abc123"
            trash.mkdir(parents=True)
            (trash / "manifest.json").write_text(json.dumps({"images": [{"image_id": 4}]}))
            result = image_storage_locations({"id": 4, "folder_id": None, "path": "../outside.webp", "thumb_path": None}, root, root / "index.db")
            self.assertFalse(result["trash"]["is_template"])
            self.assertEqual(result["trash"]["root"], str(trash.resolve()))
            self.assertTrue(result["trash"]["files"]["manifest"]["exists"])
            self.assertFalse(result["files"]["image"]["within_expected_root"])
            self.assertIsNone(result["files"]["image"]["exists"])
