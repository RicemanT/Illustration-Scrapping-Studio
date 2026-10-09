import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.db as db
from app.services import machine_tags
from app.services.tags import TagService


class MachineTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = (db.DB_PATH, db.LIBRARY_PATH)
        db.DB_PATH = self.root / "index.db"
        db.LIBRARY_PATH = self.root
        db.init_db()
        self.folder = self.root / "images" / "artist"
        self.folder.mkdir(parents=True)
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            """INSERT INTO collection (name, slug, type, query, caption_template, filters, created_at, updated_at, enabled)
               VALUES ('Artist', 'artist', 'artist', 'artist', '{}', '{}', ?, ?, 1)""", (now, now))
        self.sha = {}
        for index, tags in enumerate((("1girl", "blue_hair"), ("1boy",)), 1):
            image_path = self.folder / f"{index}.png"
            image_path.write_bytes(f"image-{index}".encode())
            self.sha[index] = hashlib.sha256(image_path.read_bytes()).hexdigest()
            conn.execute(
                """INSERT INTO image (sha256, md5, width, height, format, file_size, path, added_at, folder_id)
                   VALUES (?, '', 10, 10, 'png', 7, ?, ?, 1)""", (self.sha[index], f"artist/{index}.png", now))
            conn.execute("INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (1, ?, ?)", (index, now))
            conn.execute("""INSERT INTO image_source (image_id, provider, remote_id, fetched_at, metadata, version)
                            VALUES (?, 'danbooru', ?, ?, '{}', 1)""", (index, str(index), now))
            for tag in tags:
                conn.execute("INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (?, ?, 'general', ?)", (index, index, tag))
        conn.execute("UPDATE image SET quality_tags = ? WHERE id = 1", (json.dumps(["masterpiece"]),))
        conn.commit()
        conn.close()
        machine_tags.tagger_dir().mkdir(parents=True)

    def tearDown(self):
        db.DB_PATH, db.LIBRARY_PATH = self.old
        self.temp.cleanup()

    def sidecar(self, index):
        return (self.folder / f"{index}.txt").read_text(encoding="utf-8")

    def write_results(self, name, lines):
        (machine_tags.tagger_dir() / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_import_adds_tags_after_booru_tags_and_survives_rewrites(self):
        self.write_results("danbooru-dbv4.jsonl", [
            json.dumps({"image_id": 1, "path": "artist/1.png", "sha256": self.sha[1], "model": "dbv4",
                        "tags": [["blue hair", 0.95], ["smile", 0.8], ["open_mouth", 0.6]]}),
            # wrong id, right path: matched by path
            json.dumps({"image_id": 99, "path": "artist/2.png", "model": "dbv4", "tags": [["short_hair", 0.7]]}),
            json.dumps({"image_id": 98, "path": "artist/gone.png", "model": "dbv4", "tags": [["solo", 0.9]]}),
            "not json",
        ])
        result = machine_tags.run_import()
        self.assertEqual((result["images_updated"], result["unknown_images"], result["bad_lines"]), (2, 1, 1))
        # booru tags first, the post's own "blue hair" not repeated, quality marks still last
        self.assertEqual(self.sidecar(1), "1girl, blue hair, smile, open mouth, masterpiece")
        self.assertEqual(self.sidecar(2), "1boy, short hair")
        self.assertEqual(machine_tags.run_import()["files"], 0)  # nothing new
        self.assertEqual(machine_tags.run_import(everything=True)["unchanged"], 2)

        # removed by hand: stays removed after any rewrite or re-import
        TagService(self.root).replace_ground_truth(1, ["1girl", "blue hair", "open mouth"])
        machine_tags.run_import(everything=True)
        TagService(self.root).rewrite_all_sidecars()
        self.assertEqual(self.sidecar(1), "1girl, blue hair, open mouth, masterpiece")

        summary = machine_tags.summary()
        self.assertEqual(summary["models"]["dbv4"], {"images": 2, "tags": 4})
        cleared = machine_tags.clear("dbv4")
        self.assertEqual(cleared["images"], 2)
        self.assertEqual(self.sidecar(2), "1boy")
        self.assertEqual(machine_tags.summary()["models"], {})

    def test_general_tags_follow_the_category_policy(self):
        self.write_results("e621-hydra.jsonl", [json.dumps({"image_id": 2, "path": "artist/2.png", "model": "hydra", "tags": ["smile"]})])
        TagService(self.root).set_folder_category_policy(1, ["artist", "character"])
        machine_tags.run_import()
        self.assertEqual(self.sidecar(2), "")
        TagService(self.root).set_folder_category_policy(1, None)
        self.assertEqual(self.sidecar(2), "1boy, smile")


if __name__ == "__main__":
    unittest.main()
