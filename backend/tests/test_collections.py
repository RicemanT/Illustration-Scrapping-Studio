import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.db as db
from app.models import FolderCreate
from app.services.collections import CollectionService


class FolderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"
        db.init_db()
        self.service = CollectionService()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def test_create_stores_default_artist_tag_template(self):
        folder = self.service.create_collection(FolderCreate(name="Artist", query="artist_name"))
        self.assertEqual(folder.artist_tag_template, "Drawn by {artist}")
        self.assertEqual(folder.query, "artist name")
        conn = db.get_connection()
        stored = conn.execute(
            "SELECT artist_tag_template FROM collection WHERE id = ?", (folder.id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(stored, "Drawn by {artist}")

    def test_delete_purges_indexed_unlinked_and_unindexed_folder_files(self):
        folder = self.service.create_collection(FolderCreate(name="Delete Me", query="artist"))
        now = datetime.now(timezone.utc).isoformat()
        image_dir = self.root / "images" / folder.slug
        thumb_dir = self.root / "thumbnails" / folder.slug
        sibling_dir = self.root / "images" / "keep-me"
        image_dir.mkdir(parents=True)
        thumb_dir.mkdir(parents=True)
        sibling_dir.mkdir(parents=True)
        content = b"owned image"
        (image_dir / "owned.webp").write_bytes(content)
        (image_dir / "owned.txt").write_text("tag", encoding="utf-8")
        (image_dir / "interrupted.tmp").write_bytes(b"partial")
        (thumb_dir / "owned.jpg").write_bytes(b"thumb")
        (sibling_dir / "safe.webp").write_bytes(b"safe")

        conn = db.get_connection()
        conn.execute(
            """INSERT INTO image
               (sha256, width, height, format, file_size, path, thumb_path, added_at, folder_id)
               VALUES (?, 512, 512, 'webp', ?, ?, ?, ?, ?)""",
            (
                hashlib.sha256(content).hexdigest(), len(content),
                f"{folder.slug}/owned.webp", f"{folder.slug}/owned.jpg", now, folder.id,
            ),
        )
        conn.commit()
        conn.close()

        self.assertTrue(self.service.delete_collection(folder.id, self.root))
        self.assertFalse(image_dir.exists())
        self.assertFalse(thumb_dir.exists())
        self.assertTrue((sibling_dir / "safe.webp").exists())
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image").fetchone()[0], 0)
        conn.close()

    def test_batch_delete_moves_complete_pair_out_of_folder_and_undo_restores_it(self):
        folder = self.service.create_collection(FolderCreate(name="Recoverable", query="artist", sources=["pawchive"]))
        now = datetime.now(timezone.utc).isoformat()
        image_dir = self.root / "images" / folder.slug
        thumb_dir = self.root / "thumbnails" / folder.slug
        image_dir.mkdir(parents=True)
        thumb_dir.mkdir(parents=True)
        content = b"recoverable image"
        image_path = image_dir / "image.webp"
        sidecar_path = image_dir / "image.txt"
        thumb_path = thumb_dir / "image_thumb.jpg"
        image_path.write_bytes(content)
        sidecar_path.write_text("Drawn by artist", encoding="utf-8")
        thumb_path.write_bytes(b"thumbnail")

        conn = db.get_connection()
        conn.execute(
            """INSERT INTO image
               (sha256, width, height, format, file_size, path, thumb_path, added_at, folder_id)
               VALUES (?, 512, 512, 'webp', ?, ?, ?, ?, ?)""",
            (
                hashlib.sha256(content).hexdigest(), len(content),
                f"{folder.slug}/image.webp", f"{folder.slug}/image_thumb.jpg", now, folder.id,
            ),
        )
        image_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (?, ?, ?)",
            (folder.id, image_id, now),
        )
        conn.execute(
            "UPDATE collection_source SET last_cursor = 'old-head', backfill_cursor = '20' WHERE collection_id = ? AND provider = 'pawchive'",
            (folder.id,),
        )
        conn.commit()
        conn.close()

        token = "a" * 32
        removed = self.service.remove_images(folder.id, [image_id], self.root, token)
        self.assertEqual(len(removed), 1)
        self.assertFalse(image_path.exists())
        self.assertFalse(sidecar_path.exists())
        self.assertFalse(thumb_path.exists())
        self.assertTrue((self.root / ".trash" / token / "manifest.json").exists())
        self.assertTrue((self.root / ".trash" / token / "images" / folder.slug / "image.webp").exists())
        self.assertEqual(self.service.latest_image_recovery(folder.id, self.root)["token"], token)
        conn = db.get_connection()
        self.assertIsNone(conn.execute("SELECT folder_id FROM image WHERE id = ?", (image_id,)).fetchone()[0])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection_image WHERE image_id = ?", (image_id,)).fetchone()[0], 0)
        cursors = conn.execute(
            "SELECT last_cursor, backfill_cursor FROM collection_source WHERE collection_id = ? AND provider = 'pawchive'",
            (folder.id,),
        ).fetchone()
        self.assertEqual(tuple(cursors), (None, None))
        conn.close()

        self.assertEqual(self.service.restore_images(folder.id, token, self.root), 1)
        self.assertTrue(image_path.exists())
        self.assertTrue(sidecar_path.exists())
        self.assertTrue(thumb_path.exists())
        self.assertFalse((self.root / ".trash" / token / "manifest.json").exists())
        self.assertIsNone(self.service.latest_image_recovery(folder.id, self.root))
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT folder_id FROM image WHERE id = ?", (image_id,)).fetchone()[0], folder.id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection_image WHERE image_id = ?", (image_id,)).fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
