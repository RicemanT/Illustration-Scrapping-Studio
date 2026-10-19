"""Gelbooru tag names stored HTML-escaped (it&#039;s) are restored and re-filed under their real category."""
import app.db as db
from app.services import planner_store as store
from app.services.tag_repair import repair_escaped_gelbooru_tags
from app.services.tags import TagService
from tests.test_folder_guard import PlannerFolderCase
from tests.test_planner_delivery import planner_post

ESCAPED = "bang_dream!_it&#039;s_mygo!!!!!"
CLEAN = "bang_dream!_it's_mygo!!!!!"


class TagRepairTests(PlannerFolderCase):
    async def test_escaped_gelbooru_tags_are_restored_everywhere(self):
        main = db.get_connection()   # a small reserve, so a nearly full test disk still admits the few test images
        main.execute("INSERT OR REPLACE INTO app_setting (key, value, updated_at) VALUES ('storage_reserve_gib', '0.5', '2026-01-01')")
        main.commit()
        main.close()
        await self.deliver()
        _, ids = self.folder_images()
        image_id = ids[0]
        main = db.get_connection()
        try:
            source = main.execute(
                """INSERT INTO image_source (image_id, provider, remote_id, remote_url, source_url, fetched_at, metadata,
                   version, metadata_version, is_primary) VALUES (?, 'gelbooru', '77', '', '', '2026-01-01', '{}', 1, 1, 0)""",
                (image_id,)).lastrowid
            main.executemany("INSERT INTO image_tag (image_id, source_id, category, tag, score) VALUES (?, ?, ?, ?, NULL)",
                             [(image_id, source, "general", ESCAPED), (image_id, source, "general", "tom_&amp;_jerry"),
                              (image_id, source, "general", "smile")])
            main.execute("""INSERT INTO image_tag_override (image_id, tag, action, category, updated_at)
                            VALUES (?, 'rock_&amp;_roll', 'add', 'general', '2026-01-01')""", (image_id,))
            main.commit()
        finally:
            main.close()
        planner = store.connect()
        try:
            rows = [planner_post(900, "danbooru", "artist_x", copyrights=CLEAN),
                    planner_post(901, "gelbooru", "artist_x", general=f"1girl {ESCAPED} tom_&amp;_jerry")]
            planner.executemany(
                f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                [tuple({**r, "artist_id": 1}[c] for c in store.POST_COLUMNS) for r in rows])
            planner.commit()
        finally:
            planner.close()

        stats = repair_escaped_gelbooru_tags(self.root)
        self.assertEqual((stats["tag_rows"], stats["recategorized"], stats["overrides"], stats["planner_posts"]), (2, 1, 1, 1))
        self.assertEqual(stats["unresolved"], ["tom_&_jerry"], "no Danbooru evidence: stays general, reported")

        main = db.get_connection()
        try:
            tags = dict(main.execute("SELECT tag, category FROM image_tag WHERE source_id = ?", (source,)).fetchall())
            override = main.execute("SELECT tag FROM image_tag_override WHERE image_id = ?", (image_id,)).fetchone()[0]
        finally:
            main.close()
        self.assertEqual(tags, {CLEAN: "copyright", "tom_&_jerry": "general", "smile": "general"})
        self.assertEqual(override, "rock_&_roll")
        planner = store.connect()
        try:
            post = planner.execute("SELECT copyrights, general FROM post WHERE site = 'gelbooru' AND remote_id = '901'").fetchone()
        finally:
            planner.close()
        self.assertEqual(tuple(post), (CLEAN, "1girl tom_&_jerry"))

        truth = TagService(self.root).get_ground_truth(image_id)["tags"]
        self.assertIn("bang dream! it's mygo!!!!!", truth)
        self.assertFalse(any("&#" in tag or "&amp;" in tag for tag in truth))
        main = db.get_connection()
        try:
            path = main.execute("SELECT path FROM image WHERE id = ?", (image_id,)).fetchone()[0]
        finally:
            main.close()
        sidecar = (self.root / "images" / path).with_suffix(".txt").read_text(encoding="utf-8")
        self.assertIn("bang dream! it's mygo!!!!!", sidecar)
        self.assertNotIn("&#039;", sidecar)

        again = repair_escaped_gelbooru_tags(self.root)
        self.assertEqual((again["tag_rows"], again["overrides"], again["planner_posts"]), (0, 0, 0), "idempotent")
