import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.db as db


class FolderOwnershipMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def test_legacy_global_hash_schema_migrates_without_losing_rows(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript("""
            CREATE TABLE collection (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL, query TEXT NOT NULL, caption_template TEXT NOT NULL,
                filters TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_sync_at TEXT, enabled INTEGER DEFAULT 1
            );
            CREATE TABLE image (
                id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, md5 TEXT, phash INTEGER,
                width INTEGER NOT NULL, height INTEGER NOT NULL, format TEXT NOT NULL,
                file_size INTEGER NOT NULL, path TEXT NOT NULL, thumb_path TEXT, added_at TEXT NOT NULL
            );
            CREATE TABLE collection_image (
                collection_id INTEGER NOT NULL, image_id INTEGER NOT NULL, added_at TEXT NOT NULL,
                PRIMARY KEY (collection_id, image_id),
                FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
                FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
            );
            CREATE TABLE image_source (
                id INTEGER PRIMARY KEY, image_id INTEGER NOT NULL, provider TEXT NOT NULL,
                remote_id TEXT NOT NULL, remote_url TEXT, fetched_at TEXT NOT NULL,
                metadata TEXT NOT NULL, version INTEGER DEFAULT 1,
                UNIQUE (provider, remote_id, version),
                FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
            );
        """)
        conn.execute("INSERT INTO collection VALUES (1, 'Artist', 'artist', 'artist', 'artist', '{}', '{}', ?, ?, NULL, 1)", (now, now))
        conn.execute("INSERT INTO image VALUES (1, ?, ?, NULL, 10, 10, 'png', 12, 'artist/file.png', NULL, ?)", ("a" * 64, "b" * 32, now))
        conn.execute("INSERT INTO collection_image VALUES (1, 1, ?)", (now,))
        conn.execute("INSERT INTO image_source VALUES (1, 1, 'fixture', '7', NULL, ?, '{}', 1)", (now,))
        conn.commit()
        conn.close()

        db.init_db()
        conn = db.get_connection()
        migrated = conn.execute("SELECT sha256, folder_id, path FROM image WHERE id = 1").fetchone()
        self.assertEqual(tuple(migrated), ("a" * 64, 1, "artist/file.png"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_source WHERE image_id = 1").fetchone()[0], 1)
        migrated_filters = json.loads(conn.execute("SELECT filters FROM collection WHERE id = 1").fetchone()[0])
        self.assertEqual((migrated_filters["min_width"], migrated_filters["min_height"]), (512, 512))

        conn.execute("INSERT INTO collection (name, slug, type, query, caption_template, filters, created_at, updated_at) VALUES ('Other', 'other', 'artist', 'other', '{}', '{}', ?, ?)", (now, now))
        conn.execute("INSERT INTO image (sha256, width, height, format, file_size, path, added_at, folder_id) VALUES (?, 10, 10, 'png', 12, 'other/file.png', ?, 2)", ("a" * 64, now))
        second_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO image_source (image_id, provider, remote_id, fetched_at, metadata, version) VALUES (?, 'fixture', '7', ?, '{}', 1)", (second_id, now))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "owning folder"):
            conn.execute(
                "INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (1, ?, ?)",
                (second_id, now),
            )
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image WHERE sha256 = ?", ("a" * 64,)).fetchone()[0], 2)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
