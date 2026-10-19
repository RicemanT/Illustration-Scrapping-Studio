"""Allow-listed metadata tags join the ground truth while the meta section stays off."""
import app.db as db
from app.services import meta_tags
from app.services.tags import TagService
from tests.test_folder_guard import PlannerFolderCase


class MetaTagTests(PlannerFolderCase):
    def test_parse(self):
        text = "# header\ntraditional_media    # 12 images\n\nProcreate (medium)\n# commentary request  # 5 images\ntraditional media\n"
        self.assertEqual(meta_tags.parse(text), ["traditional media", "Procreate (medium)"])

    async def test_allowlist_in_ground_truth_and_export(self):
        main = db.get_connection()   # a small reserve, so a nearly full test disk still admits the few test images
        main.execute("INSERT OR REPLACE INTO app_setting (key, value, updated_at) VALUES ('storage_reserve_gib', '0.5', '2026-01-01')")
        main.commit()
        main.close()
        await self.deliver()
        _, ids = self.folder_images()
        main = db.get_connection()
        try:
            for image_id in ids[:2]:
                source = main.execute("SELECT id FROM image_source WHERE image_id = ?", (image_id,)).fetchone()[0]
                main.executemany("INSERT INTO image_tag (image_id, source_id, category, tag, score) VALUES (?, ?, 'meta', ?, NULL)",
                                 [(image_id, source, tag) for tag in ("highres", "traditional_media", "commentary_request")])
            main.commit()
            path = main.execute("SELECT path FROM image WHERE id = ?", (ids[0],)).fetchone()[0]
        finally:
            main.close()

        text = meta_tags.export_text()
        self.assertIn("\nhighres    # 2 images · danbooru", text)
        self.assertIn("\n# commentary request    # 2 images", text, "commentary/request come commented out")
        before = TagService(self.root).get_ground_truth(ids[0])["tags"]
        self.assertFalse({"traditional media", "highres"} & set(before), "meta off: none by default")

        result = meta_tags.apply(meta_tags.parse("traditional_media\n# highres\n"))
        self.assertEqual(result["tags"], 1)
        tags = TagService(self.root).get_ground_truth(ids[0])["tags"]
        self.assertIn("traditional media", tags)
        self.assertNotIn("highres", tags)
        self.assertNotIn("traditional media", TagService(self.root).get_ground_truth(ids[2])["tags"], "only images having it")
        general = [t for t in tags if t.startswith("tag_") or t == "1girl"]
        self.assertGreater(tags.index("traditional media"), max(tags.index(t) for t in general), "meta after general")
        sidecar = (self.root / "images" / path).with_suffix(".txt").read_text(encoding="utf-8")
        self.assertIn("traditional media", sidecar)
        self.assertIn("\ntraditional media    # 2 images", meta_tags.export_text())
        self.assertIn("\n# highres    # 2 images", meta_tags.export_text(), "with a list: the rest commented")
