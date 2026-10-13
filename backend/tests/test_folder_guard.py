"""Curation guard: accepted folders take no new images, and posts removed by hand stay removed."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import app.db as db
from app.services import folder_guard
from app.services import planner_curation as curation
from app.services import planner_delivery as delivery
from app.services import planner_store as store
from app.services.collections import CollectionService
from app.services.images import ImageService
from app.services.planner_select import PlannerConfig
from tests.test_planner_delivery import FakeSite, planner_post


class FolderGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        from pathlib import Path
        self.root = Path(self.temp.name)
        self.old = (db.DB_PATH, db.LIBRARY_PATH, os.environ.get('ARTIST_PLANNER_PATH'))
        db.DB_PATH = self.root / 'index.db'
        db.LIBRARY_PATH = self.root
        os.environ['ARTIST_PLANNER_PATH'] = str(self.root / 'planner')
        db.init_db()
        store.import_artists('site,display_name,query_tag\ndanbooru,Artist X,artist_x\n')
        conn = store.connect()
        rows = [planner_post(i, 'danbooru', 'artist_x') for i in range(1, 7)]
        conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                         [tuple({**r, 'artist_id': 1}[c] for c in store.POST_COLUMNS) for r in rows])
        conn.commit()
        conn.close()
        self.run_id = store.run_plan(PlannerConfig(min_images=3, max_images=5, character_floor=0))

    def tearDown(self):
        db.DB_PATH, db.LIBRARY_PATH, planner_path = self.old
        if planner_path is None:
            os.environ.pop('ARTIST_PLANNER_PATH', None)
        else:
            os.environ['ARTIST_PLANNER_PATH'] = planner_path
        self.temp.cleanup()

    async def deliver(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), lambda site: FakeSite(site), self.root)
        return job['id']

    def folder_images(self):
        main = db.get_connection()
        try:
            folder_id = main.execute('SELECT id FROM collection').fetchone()[0]
            ids = [r[0] for r in main.execute('SELECT id FROM image WHERE folder_id=? ORDER BY id', (folder_id,))]
            return folder_id, ids
        finally:
            main.close()

    def source_of(self, image_id):
        main = db.get_connection()
        try:
            return main.execute('SELECT remote_id FROM image_source WHERE image_id=?', (image_id,)).fetchone()[0]
        finally:
            main.close()

    async def test_removed_posts_are_not_delivered_again_until_undone(self):
        await self.deliver()
        folder_id, ids = self.folder_images()
        removed = ids[0]
        remote_id = self.source_of(removed)
        CollectionService().remove_images(folder_id, [removed], self.root, 'aa' * 16)
        main = db.get_connection()
        self.assertTrue(folder_guard.removed_post(main, folder_id, 'danbooru', remote_id))
        main.close()

        await self.deliver()   # the same run again: the removed post must stay out
        self.assertEqual(len(self.folder_images()[1]), len(ids) - 1)

        CollectionService().restore_images(folder_id, 'aa' * 16, self.root)
        main = db.get_connection()
        self.assertFalse(folder_guard.removed_post(main, folder_id, 'danbooru', remote_id))
        main.close()
        self.assertEqual(len(self.folder_images()[1]), len(ids))

    async def test_accepted_folder_takes_no_new_images_from_any_path(self):
        await self.deliver()
        folder_id, ids = self.folder_images()
        CollectionService().remove_images(folder_id, [ids[0]], self.root, 'bb' * 16)
        main = db.get_connection()
        folder_guard.clear_posts(main, folder_id, 'danbooru', [self.source_of(ids[0])])   # as if imported by hand later
        main.commit()
        main.close()
        curation.set_complete(folder_id, True)
        main = db.get_connection()
        self.assertTrue(folder_guard.frozen_at(main, folder_id))
        main.execute('UPDATE collection SET frozen_at=NULL')   # as if accepted before the guard existed
        main.commit()
        main.close()
        self.assertEqual(curation.sync_frozen(), 1)   # startup freezes it
        main = db.get_connection()
        self.assertTrue(folder_guard.frozen_at(main, folder_id))
        main.close()

        await self.deliver()   # delivery: skipped, although the post is no longer marked removed
        self.assertEqual(len(self.folder_images()[1]), len(ids) - 1)

        # The write point itself refuses, whatever the caller.
        site = FakeSite('danbooru')
        post = site.post('6')
        path = str(self.root / 'tmp6.jpg')
        await site.download_image(post, path)
        with self.assertRaises(folder_guard.FolderFrozen):
            ImageService(self.root).ingest_image(path, post, folder_id)

        # Manual folder sync is refused.
        from app.routes import sync as sync_routes
        main = db.get_connection()
        main.execute("INSERT OR IGNORE INTO collection_source (collection_id, provider, enabled) VALUES (?, 'danbooru', 1)", (folder_id,))
        main.commit()
        main.close()
        with self.assertRaises(HTTPException) as caught:
            await sync_routes.sync_folder(folder_id, 'danbooru')
        self.assertEqual(caught.exception.status_code, 409)

        curation.set_complete(folder_id, False)   # reopened: open again
        main = db.get_connection()
        self.assertIsNone(folder_guard.frozen_at(main, folder_id))
        main.close()

    async def test_late_images_are_found_and_removable(self):
        await self.deliver()
        folder_id, ids = self.folder_images()
        curation.set_complete(folder_id, True)
        later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        main = db.get_connection()
        main.execute('UPDATE image SET added_at=? WHERE id=?', (later, ids[-1]))   # a leak that arrived after acceptance
        main.commit()
        main.close()
        self.assertEqual(curation.late_images([folder_id]), {folder_id: [ids[-1]]})
        result = curation.remove_late_images(folder_id, self.root)
        self.assertEqual(result['removed'], 1)
        self.assertTrue(result['undo_token'])
        self.assertEqual(curation.late_images([folder_id]), {})
        self.assertEqual(self.folder_images()[1], ids[:-1])

    def test_frames_are_remembered_by_post(self):
        main = db.get_connection()
        try:
            main.execute("INSERT INTO collection (id, name, slug, type, query, caption_template, filters, created_at, updated_at) "
                         "VALUES (99, 'f', 'f', 'artist', 'q', '', '{}', 'now', 'now')")
            folder_guard.record_removed(main, 99, [('danbooru', '123:frame:2')])
            # Deleting one frame protects the whole post: the next sync must not re-extract the missing frames.
            self.assertTrue(folder_guard.removed_post(main, 99, 'danbooru', '123'))
            self.assertIsNotNone(folder_guard.skip_reason(main, 99, 'danbooru', '123', automatic=True))
            self.assertIsNone(folder_guard.skip_reason(main, 99, 'danbooru', '123', automatic=False))
            folder_guard.clear_posts(main, 99, 'danbooru', ['123'])   # imported by hand
            self.assertFalse(folder_guard.removed_post(main, 99, 'danbooru', '123'))
            self.assertEqual(main.execute('SELECT count(*) FROM folder_removed_source').fetchone()[0], 0)
        finally:
            main.close()


if __name__ == '__main__':
    unittest.main()
