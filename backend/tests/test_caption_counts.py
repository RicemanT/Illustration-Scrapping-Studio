import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.db as db
from app.services import captions


class CaptionCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = (db.DB_PATH, db.LIBRARY_PATH)
        db.DB_PATH, db.LIBRARY_PATH = self.root / "index.db", self.root
        db.init_db()
        captions._counts_cache.update(at=0.0, value=None)
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        for folder_id, name in ((1, "alpha"), (2, "beta")):
            conn.execute("""INSERT INTO collection (id, name, slug, type, query, caption_template, filters, created_at, updated_at, enabled)
                            VALUES (?, ?, ?, 'artist', ?, '{}', '{}', ?, ?, 1)""", (folder_id, name, name, name, now, now))
            folder = self.root / "images" / "group" / name
            folder.mkdir(parents=True)
            for index in range(3):
                (folder / f"{index}.png").write_bytes(b"x")
                conn.execute("""INSERT INTO image (sha256, width, height, format, file_size, path, added_at, folder_id)
                                VALUES (?, 1, 1, 'png', 1, ?, ?, ?)""", (f"{name}{index}", f"group/{name}/{index}.png", now, folder_id))
        conn.commit()
        conn.close()
        alpha = self.root / "images" / "group" / "alpha"
        (alpha / "0_nl.txt").write_text("caption", encoding="utf-8")
        (alpha / "1_nl.txt").write_text("caption", encoding="utf-8")
        (alpha / "2.txt").write_text("tags only, not a caption", encoding="utf-8")

    def tearDown(self):
        db.DB_PATH, db.LIBRARY_PATH = self.old
        self.temp.cleanup()

    def test_counts_caption_files_per_folder(self):
        self.assertEqual(captions.caption_counts(), {"suffix": "_nl.txt", "folders": {1: 2, 2: 0}})


if __name__ == "__main__":
    unittest.main()
