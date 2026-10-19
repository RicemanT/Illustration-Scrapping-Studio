"""Switching the artist trigger format of all artist folders: template, tag sidecars and captions, and back."""
import app.db as db
from app.services import artist_trigger
from app.services.captions import caption_file, get_suffix
from app.services.caption_check import explain_caption
from app.services.tags import TagService
from tests.test_folder_guard import PlannerFolderCase


class ArtistTriggerTests(PlannerFolderCase):
    def test_recaption_forms(self):
        old = ["Drawn by artist x"]
        self.assertEqual(artist_trigger.recaption("Drawn by artist x. A girl.", old, "@artist x"), "@artist x. A girl.")
        self.assertEqual(artist_trigger.recaption("drawn by Artist X, a girl.", old, "@artist x"), "@artist x. a girl.")
        self.assertEqual(artist_trigger.recaption("A girl smiles.", old, "@artist x"), "@artist x. A girl smiles.")
        self.assertEqual(artist_trigger.recaption("Drawn by an alias. A girl.", old, "@artist x"), "@artist x. A girl.")
        self.assertEqual(artist_trigger.recaption("@artist x. A girl.", ["@artist x"], "Drawn by artist x"),
                         "Drawn by artist x. A girl.")
        with self.assertRaises(ValueError):
            artist_trigger.validate_template("@artist")

    async def test_switch_and_back(self):
        main = db.get_connection()   # a small reserve, so a nearly full test disk still admits the few test images
        main.execute("INSERT OR REPLACE INTO app_setting (key, value, updated_at) VALUES ('storage_reserve_gib', '0.5', '2026-01-01')")
        main.commit()
        main.close()
        await self.deliver()
        folder_id, ids = self.folder_images()
        main = db.get_connection()
        try:
            name, template = main.execute("SELECT name, artist_tag_template FROM collection WHERE id = ?", (folder_id,)).fetchone()
            paths = dict(main.execute(f"SELECT id, path FROM image WHERE folder_id = ?", (folder_id,)).fetchall())
        finally:
            main.close()
        drawn = artist_trigger.render(template or "Drawn by {artist}", name)
        at = artist_trigger.render("@{artist}", name)
        captions = {image_id: caption_file(self.root / "images" / paths[image_id], get_suffix()) for image_id in ids[:3]}
        captions[ids[0]].write_text(drawn + ". A girl waves at the viewer.\n", encoding="utf-8")
        captions[ids[1]].write_text("A girl reads a book.", encoding="utf-8")            # MOSS-style, no trigger
        captions[ids[2]].write_text("Drawn by some alias. A cat sleeps.", encoding="utf-8")

        preview = artist_trigger.preview("@{artist}")
        self.assertEqual((preview["folders"], preview["example"]["after"]), (1, at))
        stats = artist_trigger.apply("@{artist}")
        self.assertEqual((stats["captions_changed"], stats["captions_added"]), (2, 1))
        self.assertEqual(TagService(self.root).get_ground_truth(ids[0])["tags"][0], at)
        sidecar = (self.root / "images" / paths[ids[0]]).with_suffix(".txt").read_text(encoding="utf-8")
        self.assertTrue(sidecar.startswith(at + ","), sidecar)
        self.assertEqual(captions[ids[0]].read_text(encoding="utf-8"), at + ". A girl waves at the viewer.\n")
        self.assertEqual(captions[ids[1]].read_text(encoding="utf-8"), at + ". A girl reads a book.")
        self.assertEqual(captions[ids[2]].read_text(encoding="utf-8"), at + ". A cat sleeps.")
        problems = explain_caption(captions[ids[1]].read_text(encoding="utf-8"), [at, "1girl"], {}, 1, 500)
        self.assertNotIn("artist_start", problems)
        self.assertIn("artist_start", explain_caption("A girl.", [at], {}, 1, 500), "the check knows @ triggers")

        artist_trigger.apply("Drawn by {artist}")   # and back
        self.assertEqual(TagService(self.root).get_ground_truth(ids[0])["tags"][0], drawn)
        self.assertEqual(captions[ids[1]].read_text(encoding="utf-8"), drawn + ". A girl reads a book.")
