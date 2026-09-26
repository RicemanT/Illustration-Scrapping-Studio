import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

import app.db as db
from app.services.dedup import DedupService, color_distance, compute_fingerprint, hamming_distance
from app.services.tags import TagService


def make_artwork(path: Path, size=(320, 240), quality=None) -> None:
    image = Image.new("RGB", size, (238, 220, 200))
    draw = ImageDraw.Draw(image)
    draw.rectangle((size[0] // 8, size[1] // 7, size[0] * 7 // 8, size[1] * 6 // 7), fill=(30, 70, 130))
    draw.ellipse((size[0] // 3, size[1] // 5, size[0] * 3 // 4, size[1] * 4 // 5), fill=(230, 100, 70))
    draw.line((0, size[1] - 1, size[0] - 1, 0), fill=(250, 245, 230), width=max(2, size[0] // 50))
    if quality is None:
        image.save(path)
    else:
        image.save(path, "JPEG", quality=quality)
    image.close()


class FingerprintTests(unittest.TestCase):
    def test_resized_recompressed_artwork_remains_close(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.png"
            derivative = Path(folder) / "derivative.jpg"
            make_artwork(original)
            make_artwork(derivative, size=(160, 120), quality=65)
            first = compute_fingerprint(original)
            second = compute_fingerprint(derivative)
            self.assertLessEqual(hamming_distance(first.phash, second.phash), 10)
            self.assertLessEqual(hamming_distance(first.dhash, second.dhash), 12)
            self.assertLessEqual(color_distance(first.colorhash, second.colorhash), 8)

    def test_color_signature_rejects_luminance_hash_collision(self):
        with tempfile.TemporaryDirectory() as folder:
            red_path = Path(folder) / "red.png"
            blue_path = Path(folder) / "blue.png"
            Image.new("RGB", (100, 100), "red").save(red_path)
            Image.new("RGB", (100, 100), "blue").save(blue_path)
            red = compute_fingerprint(red_path)
            blue = compute_fingerprint(blue_path)
            self.assertEqual(hamming_distance(red.phash, blue.phash), 0)
            self.assertGreater(color_distance(red.colorhash, blue.colorhash), 60)


class DedupServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"
        db.init_db()
        self.images = self.root / "images" / "first"
        self.thumbs = self.root / "thumbnails" / "first"
        self.images.mkdir(parents=True)
        self.thumbs.mkdir(parents=True)
        self.service = DedupService(self.root)
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        for name, slug in (("First", "first"), ("Second", "second")):
            conn.execute(
                """INSERT INTO collection
                   (name, slug, type, query, caption_template, filters, created_at, updated_at, enabled)
                   VALUES (?, ?, 'artist', ?, '{}', '{}', ?, ?, 1)""",
                (name, slug, name, now, now),
            )
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def _insert_image(self, filename: str, collection_id: int, size, quality=None) -> int:
        path = self.images / filename
        make_artwork(path, size=size, quality=quality)
        payload = path.read_bytes()
        now = datetime.now(timezone.utc).isoformat()
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format.lower()
        conn = db.get_connection()
        conn.execute(
            """INSERT INTO image
               (sha256, md5, width, height, format, file_size, path, added_at, folder_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                hashlib.sha256(payload).hexdigest(), hashlib.md5(payload).hexdigest(), width, height,
                image_format, len(payload), f"first/{filename}", now, collection_id,
            ),
        )
        image_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (?, ?, ?)",
            (collection_id, image_id, now),
        )
        conn.execute(
            """INSERT INTO image_source
               (image_id, provider, remote_id, fetched_at, metadata, version)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (image_id, "danbooru", str(image_id), now, json.dumps({"image": image_id})),
        )
        source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (?, ?, 'general', ?)",
            (image_id, source_id, f"tag {image_id}"),
        )
        conn.commit()
        conn.close()
        return image_id

    def test_scan_backfills_and_merge_preserves_folder_metadata(self):
        first_id = self._insert_image("large.png", 1, (320, 240))
        second_id = self._insert_image("small.jpg", 1, (160, 120), quality=65)
        conn = db.get_connection()
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """INSERT INTO image_tag_override
               (image_id, tag, action, category, updated_at)
               VALUES (?, ?, ?, 'general', ?)""",
            [
                (first_id, "winner added", "add", now),
                (second_id, f"tag {second_id}", "remove", now),
                (second_id, "loser added", "add", now),
            ],
        )
        conn.commit()
        conn.close()

        result = self.service.scan(collection_id=1, profile="balanced")
        self.assertEqual(result["fingerprints_added"], 2)
        candidates = self.service.list_candidates(collection_id=1)
        self.assertEqual(candidates["total"], 1)
        candidate = candidates["items"][0]
        resolution = self.service.resolve(candidate["id"], "keep_highest_quality")
        self.assertEqual(resolution["kept_image_id"], first_id)
        self.assertEqual(resolution["removed_image_id"], second_id)

        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_source WHERE image_id = ?", (first_id,)).fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_tag WHERE image_id = ?", (first_id,)).fetchone()[0], 2)
        memberships = {row[0] for row in conn.execute(
            "SELECT collection_id FROM collection_image WHERE image_id = ?", (first_id,)
        ).fetchall()}
        self.assertEqual(memberships, {1})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM duplicate_resolution").fetchone()[0], 1)
        conn.close()
        self.assertEqual(
            TagService(self.root).get_ground_truth(first_id)["tags"],
            [f"tag {first_id}", "winner added", "loser added"],
        )
        sidecar = self.root / "images" / "first" / "large.txt"
        self.assertEqual(sidecar.read_text(encoding="utf-8"), f"tag {first_id}, winner added, loser added")

    def test_visually_identical_images_in_different_folders_are_not_candidates(self):
        self._insert_image("folder-one.png", 1, (320, 240))
        self._insert_image("folder-two.png", 2, (320, 240))
        result = self.service.scan(collection_id=1, profile="balanced")
        self.assertEqual(result["images_scanned"], 1)
        self.assertEqual(result["candidates_found"], 0)
        self.assertEqual(self.service.list_candidates(collection_id=1)["total"], 0)


if __name__ == "__main__":
    unittest.main()
