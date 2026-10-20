"""Accepted folders' sidecars carry their current quality marks, even when a job changed the marks afterwards."""
import app.db as db
from app.services import planner_curation as curation
from app.services.tags import TagService
from tests.test_folder_guard import PlannerFolderCase


class AcceptedMarksTests(PlannerFolderCase):
    async def test_marks_changed_after_acceptance_are_committed(self):
        main = db.get_connection()   # a small reserve, so a nearly full test disk still admits the few test images
        main.execute("INSERT OR REPLACE INTO app_setting (key, value, updated_at) VALUES ('storage_reserve_gib', '0.5', '2026-01-01')")
        main.commit()
        main.close()
        await self.deliver()
        folder_id, ids = self.folder_images()
        curation.set_complete(folder_id, True)

        def set_mark(image_id, mark):   # what the automatic quality job does, without committing
            main = db.get_connection()
            try:
                main.execute("UPDATE image SET quality_mark=?, quality_source='auto' WHERE id=?", (mark, image_id))
                main.commit()
            finally:
                main.close()

        set_mark(ids[0], "masterpiece")
        self.assertNotIn("masterpiece", TagService(self.root).get_ground_truth(ids[0])["tags"], "the bug: marked, not committed")
        self.assertEqual(curation.sync_accepted_marks(), 1)
        tags = TagService(self.root).get_ground_truth(ids[0])["tags"]
        self.assertEqual(tags[-1], "masterpiece")
        main = db.get_connection()
        try:
            path = main.execute("SELECT path FROM image WHERE id=?", (ids[0],)).fetchone()[0]
        finally:
            main.close()
        self.assertTrue((self.root / "images" / path).with_suffix(".txt").read_text(encoding="utf-8").endswith(", masterpiece"))
        self.assertEqual(curation.sync_accepted_marks(), 0, "nothing left to commit")

        curation.set_complete(folder_id, False)   # reopened: marks wait for the next acceptance
        set_mark(ids[1], "best quality")
        self.assertEqual(curation.sync_accepted_marks(), 0)
        self.assertNotIn("best quality", TagService(self.root).get_ground_truth(ids[1])["tags"])
