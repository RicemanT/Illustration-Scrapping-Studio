import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.db as db
from app.services.tags import TagService, normalize_tags


class TagServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db_path = db.DB_PATH
        db.DB_PATH = self.root / "index.db"
        db.init_db()
        self.folder = self.root / "images" / "artist"
        self.folder.mkdir(parents=True)
        self.service = TagService(self.root)
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            """INSERT INTO collection
               (name, slug, type, query, caption_template, filters, created_at, updated_at, enabled)
               VALUES ('Artist', 'artist', 'artist', 'artist', '{}', '{}', ?, ?, 1)""",
            (now, now),
        )
        for index, tags in enumerate((("1girl", "blue_hair"), ("1girl", "solo")), 1):
            image_path = self.folder / f"{index}.png"
            image_path.write_bytes(f"image-{index}".encode())
            content = image_path.read_bytes()
            conn.execute(
                """INSERT INTO image
                   (sha256, md5, width, height, format, file_size, path, added_at, folder_id)
                   VALUES (?, ?, 10, 10, 'png', ?, ?, ?, 1)""",
                (hashlib.sha256(content).hexdigest(), hashlib.md5(content).hexdigest(), len(content), f"artist/{index}.png", now),
            )
            image_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO collection_image (collection_id, image_id, added_at) VALUES (1, ?, ?)", (image_id, now))
            conn.execute(
                """INSERT INTO image_source (image_id, provider, remote_id, fetched_at, metadata, version)
                   VALUES (?, 'danbooru', ?, ?, ?, 1)""",
                (image_id, str(index), now, json.dumps({"tags": tags})),
            )
            source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for tag in tags:
                conn.execute(
                    "INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (?, ?, 'general', ?)",
                    (image_id, source_id, tag),
                )
        conn.commit()
        conn.close()

    def tearDown(self):
        db.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def test_replace_preserves_source_tags_and_undo_restores_sidecar(self):
        result = self.service.replace_ground_truth(1, ["blue hair", "masterpiece"])
        self.assertEqual(result["tags"], ["blue hair", "masterpiece"])
        self.assertNotIn("_", (self.folder / "1.txt").read_text(encoding="utf-8"))
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_tag WHERE image_id = 1").fetchone()[0], 2)
        conn.close()

        self.service.undo(result["undo_token"])
        restored = self.service.get_ground_truth(1)
        self.assertEqual(restored["tags"], ["1girl", "blue hair"])
        self.assertEqual((self.folder / "1.txt").read_text(encoding="utf-8"), "1girl, blue hair")

    def test_batch_add_remove_and_undo(self):
        added = self.service.bulk_edit(1, [1, 2], ["high_quality"], "add")
        self.assertEqual(added["changed_images"], 2)
        self.assertIn("high quality", self.service.get_ground_truth(1)["tags"])
        removed = self.service.bulk_edit(1, [1, 2], ["1girl"], "remove")
        self.assertEqual(removed["changed_images"], 2)
        self.assertNotIn("1girl", self.service.get_ground_truth(2)["tags"])
        self.service.undo(removed["undo_token"])
        self.assertIn("1girl", self.service.get_ground_truth(1)["tags"])
        self.assertIn("high quality", self.service.get_ground_truth(2)["tags"])

    def test_batch_replace_updates_only_matches_preserves_sources_and_is_undoable(self):
        result = self.service.bulk_replace(1, [1, 2], "blue_hair", "Drawn by diives")
        self.assertEqual(result["requested_images"], 2)
        self.assertEqual(result["matched_images"], 1)
        self.assertEqual(result["changed_images"], 1)
        self.assertNotIn("blue hair", self.service.get_ground_truth(1)["tags"])
        self.assertIn("Drawn by diives", self.service.get_ground_truth(1)["tags"])
        self.assertEqual(self.service.get_ground_truth(2)["tags"], ["1girl", "solo"])
        self.assertEqual((self.folder / "1.txt").read_text(encoding="utf-8"), "1girl, Drawn by diives")

        conn = db.get_connection()
        source_tags = [row[0] for row in conn.execute("SELECT tag FROM image_tag WHERE image_id = 1 ORDER BY rowid")]
        conn.close()
        self.assertEqual(source_tags, ["1girl", "blue_hair"])

        self.service.undo(result["undo_token"])
        self.assertEqual(self.service.get_ground_truth(1)["tags"], ["1girl", "blue hair"])
        self.assertEqual((self.folder / "1.txt").read_text(encoding="utf-8"), "1girl, blue hair")

    def test_batch_replace_preserves_trigger_position(self):
        result = self.service.bulk_replace(1, [1, 2], "1girl", "Drawn by diives")
        self.assertEqual(result["changed_images"], 2)
        self.assertEqual(
            self.service.get_ground_truth(1)["tags"],
            ["Drawn by diives", "blue hair"],
        )
        self.assertEqual(
            self.service.get_ground_truth(2)["tags"],
            ["Drawn by diives", "solo"],
        )
        self.assertEqual(
            (self.folder / "1.txt").read_text(encoding="utf-8"),
            "Drawn by diives, blue hair",
        )
        self.service.undo(result["undo_token"])
        self.assertEqual(self.service.get_ground_truth(1)["tags"], ["1girl", "blue hair"])

    def test_rerun_repairs_positionless_replacements_from_previous_build(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute(
            """INSERT INTO image_tag_override (image_id, tag, action, category, updated_at)
               VALUES (1, '1girl', 'remove', 'general', ?)""",
            (timestamp,),
        )
        conn.execute(
            """INSERT INTO image_tag_override (image_id, tag, action, category, updated_at)
               VALUES (1, 'Drawn by diives', 'add', 'general', ?)""",
            (timestamp,),
        )
        conn.commit()
        conn.close()
        self.service._rewrite_sidecar(1)
        self.assertEqual((self.folder / "1.txt").read_text(encoding="utf-8"), "blue hair, Drawn by diives")

        result = self.service.bulk_replace(1, [1], "1girl", "Drawn by diives")
        self.assertEqual(result["matched_images"], 1)
        self.assertEqual(result["changed_images"], 1)
        self.assertEqual(self.service.get_ground_truth(1)["tags"], ["Drawn by diives", "blue hair"])
        self.assertEqual((self.folder / "1.txt").read_text(encoding="utf-8"), "Drawn by diives, blue hair")

    def test_batch_replace_rejects_same_tag_and_no_match_creates_no_undo(self):
        with self.assertRaises(ValueError):
            self.service.bulk_replace(1, [1], "blue_hair", "blue hair")
        result = self.service.bulk_replace(1, [1, 2], "not present", "replacement")
        self.assertEqual(result["matched_images"], 0)
        self.assertEqual(result["changed_images"], 0)
        self.assertIsNone(result["undo_token"])

    def test_normalization_never_exposes_underscores(self):
        self.assertEqual(normalize_tags(["blue_hair", " blue   hair ", "solo"]), ["blue hair", "solo"])

    def test_artist_template_uses_lowercase_folder_name_without_mutating_provenance(self):
        conn = db.get_connection()
        source_id = conn.execute("SELECT id FROM image_source WHERE image_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE collection SET artist_tag_template = 'Drawn by {artist}' WHERE id = 1")
        conn.execute(
            "INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, 'artist', 'alice_artist')",
            (source_id,),
        )
        conn.execute(
            "INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, 'artist', 'bob_artist')",
            (source_id,),
        )
        conn.commit()
        conn.close()

        self.service._rewrite_sidecar(1)
        self.assertEqual(
            self.service.get_ground_truth(1)["tags"],
            ["Drawn by artist", "1girl", "blue hair"],
        )
        self.assertEqual(
            (self.folder / "1.txt").read_text(encoding="utf-8"),
            "Drawn by artist, 1girl, blue hair",
        )
        conn = db.get_connection()
        raw_artist_tags = [row[0] for row in conn.execute(
            "SELECT tag FROM image_tag WHERE image_id = 1 AND category = 'artist' ORDER BY rowid"
        )]
        conn.close()
        self.assertEqual(raw_artist_tags, ["alice_artist", "bob_artist"])

    def test_effective_tags_follow_canonical_category_order(self):
        conn = db.get_connection()
        source_id = conn.execute("SELECT id FROM image_source WHERE image_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute(
            """UPDATE collection
               SET artist_tag_template = 'Drawn by {artist}',
                   ground_truth_categories = '["artist","character","copyright","species","general","meta"]'
               WHERE id = 1"""
        )
        # Insert deliberately out of order to prove row order cannot leak into
        # trainer-facing sidecars.
        for category, tag in (
            ("meta", "highres"),
            ("general", "2boys"),
            ("copyright", "example_series"),
            ("species", "human"),
            ("character", "example_character"),
        ):
            conn.execute(
                "INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, ?, ?)",
                (source_id, category, tag),
            )
        conn.commit()
        conn.close()

        self.service._rewrite_sidecar(1)
        self.assertEqual(self.service.get_ground_truth(1)["tags"], [
            "Drawn by artist",
            "example character",
            "example series",
            "human",
            "1girl", "blue hair", "2boys",
            "highres",
        ])

        self.service.bulk_edit(1, [1], ["manual_general"], "add")
        self.assertEqual(self.service.get_ground_truth(1)["tags"], [
            "Drawn by artist",
            "example character",
            "example series",
            "human",
            "1girl", "blue hair", "2boys", "manual general",
            "highres",
        ])

    def test_untrusted_provider_tags_are_provenance_only_but_folder_trigger_remains(self):
        conn = db.get_connection()
        conn.execute("UPDATE collection SET name = 'HerHeim', artist_tag_template = 'Drawn by {artist}' WHERE id = 1")
        source_id = conn.execute("SELECT id FROM image_source WHERE image_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE image_source SET provider = 'pixiv' WHERE id = ?", (source_id,))
        conn.execute("INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, 'artist', 'Herheim@お仕事募集中')", (source_id,))
        conn.execute("INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, 'general', 'ELDENRING')", (source_id,))
        conn.commit()
        conn.close()

        self.service._rewrite_sidecar(1)
        self.assertEqual(self.service.get_ground_truth(1)["tags"], ["Drawn by herheim"])
        self.assertEqual((self.folder / "1.txt").read_text(encoding="utf-8"), "Drawn by herheim")
        conn = db.get_connection()
        raw = [row[0] for row in conn.execute("SELECT tag FROM image_tag WHERE image_id = 1 ORDER BY rowid")]
        conn.close()
        self.assertIn("Herheim@お仕事募集中", raw)
        self.assertIn("ELDENRING", raw)

    def test_deviantart_tags_are_provenance_only(self):
        conn = db.get_connection()
        conn.execute("UPDATE collection SET name = 'Artist', artist_tag_template = 'Drawn by {artist}' WHERE id = 1")
        source_id = conn.execute("SELECT id FROM image_source WHERE image_id = 2 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute("UPDATE image_source SET provider = 'deviantart' WHERE id = ?", (source_id,))
        conn.commit()
        conn.close()
        self.assertEqual(self.service.get_ground_truth(2)["tags"], ["Drawn by artist"])

    def test_global_and_folder_category_policies_rewrite_sidecars_but_preserve_source_tags(self):
        conn = db.get_connection()
        source_id = conn.execute("SELECT id FROM image_source WHERE image_id = 1 ORDER BY id LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (1, ?, 'meta', 'highres')",
            (source_id,),
        )
        conn.execute("UPDATE image SET folder_id = 1 WHERE id IN (1, 2)")
        conn.commit()
        conn.close()

        self.assertNotIn("highres", self.service.get_ground_truth(1)["tags"])
        global_result = self.service.set_global_category_policy(["artist", "character", "copyright", "general", "meta"])
        self.assertEqual(global_result["updated_images"], 2)
        self.assertIn("highres", self.service.get_ground_truth(1)["tags"])
        self.assertTrue((self.folder / "1.txt").read_text(encoding="utf-8").endswith("highres"))

        folder_result = self.service.set_folder_category_policy(1, ["artist", "character", "copyright", "general"])
        self.assertFalse(folder_result["inherits_global"])
        self.assertNotIn("highres", self.service.get_ground_truth(1)["tags"])
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_tag WHERE image_id = 1 AND category = 'meta'").fetchone()[0], 1)
        conn.close()

        inherited = self.service.set_folder_category_policy(1, None)
        self.assertTrue(inherited["inherits_global"])
        self.assertIn("highres", self.service.get_ground_truth(1)["tags"])

    def test_legacy_e621_species_and_policies_are_migrated_once(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = db.get_connection()
        conn.execute("DELETE FROM app_setting WHERE key = 'ground_truth_species_v1'")
        conn.execute(
            "UPDATE app_setting SET value = '[\"artist\",\"character\",\"copyright\",\"general\"]' "
            "WHERE key = 'ground_truth_categories'"
        )
        conn.execute(
            "UPDATE collection SET ground_truth_categories = '[\"artist\",\"copyright\",\"general\"]' WHERE id = 1"
        )
        conn.execute(
            """INSERT INTO image_source (image_id, provider, remote_id, fetched_at, metadata, version)
               VALUES (1, 'e621', 'species-test', ?, ?, 1)""",
            (now, json.dumps({"tags": {"species": ["canine", "mammal"]}})),
        )
        conn.commit()
        conn.close()

        db.init_db()
        conn = db.get_connection()
        global_policy = json.loads(conn.execute(
            "SELECT value FROM app_setting WHERE key = 'ground_truth_categories'"
        ).fetchone()[0])
        folder_policy = json.loads(conn.execute(
            "SELECT ground_truth_categories FROM collection WHERE id = 1"
        ).fetchone()[0])
        species = [row[0] for row in conn.execute(
            "SELECT tag FROM image_tag WHERE image_id = 1 AND category = 'species' ORDER BY rowid"
        )]
        marker_count = conn.execute(
            "SELECT COUNT(*) FROM app_setting WHERE key = 'ground_truth_species_v1'"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(global_policy, ["artist", "character", "copyright", "species", "general"])
        self.assertEqual(folder_policy, ["artist", "copyright", "species", "general"])
        self.assertEqual(species, ["canine", "mammal"])
        self.assertEqual(marker_count, 1)


if __name__ == "__main__":
    unittest.main()
