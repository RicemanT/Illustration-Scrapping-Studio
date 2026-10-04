import asyncio
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import app.db as db
from app.services import planner_delivery as delivery
from app.services import planner_store as store
from app.services.planner_select import PlannerConfig
import test_planner
import test_planner_delivery

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import planner_style_check as style  # noqa: E402


class EnableOnlyTests(unittest.TestCase):
    setUp = test_planner.PlannerStoreTests.setUp
    tearDown = test_planner.PlannerStoreTests.tearDown

    def test_enable_only_limits_scope_and_enable_all_restores_the_list(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,Artist A,artist_a\ndanbooru,b,artist_b\n'
                             'e621,Artist A,artist_a\ngelbooru,c,artist_c\n')
        result = store.enable_only(['e621,artist_a', 'artist b', 'nobody'])
        self.assertEqual((result['enabled'], result['unknown']), (2, ['nobody']))
        enabled = {(a['site'], a['tag']) for a in store.list_artists()['items']}
        self.assertEqual(enabled, {('e621', 'artist_a'), ('danbooru', 'artist_b')})
        # A name on two sites without a site prefix enables both and is reported.
        result = store.enable_only(['Artist A'])
        self.assertEqual((result['enabled'], result['ambiguous']), (2, ['Artist A']))
        self.assertEqual(store.enable_all(), 2)
        self.assertEqual(store.list_artists()['total'], 4)

    def test_enable_all_does_not_revive_artists_removed_from_the_csv(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,a,artist_a\ndanbooru,b,artist_b\n')
        store.import_artists('site,display_name,query_tag\ndanbooru,a,artist_a\n')
        store.enable_only(['artist_a'])
        store.enable_all()
        self.assertEqual([a['tag'] for a in store.list_artists()['items']], ['artist_a'])
        self.assertEqual(store.status()['listed_artists'], 1)
        self.assertEqual(store.enable_only(['artist_b'])['enabled'], 0)


class PruneTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_planner_delivery.DeliveryTests.setUp
    tearDown = test_planner_delivery.DeliveryTests.tearDown
    factory = test_planner_delivery.DeliveryTests.factory

    async def test_banned_image_is_pruned_after_a_new_run_and_manual_images_are_kept(self):
        first = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(first['id'], asyncio.Event(), self.factory, self.root)
        conn = store.connect()
        banned = conn.execute("SELECT remote_id FROM selection WHERE run_id=? AND site='danbooru' AND remote_id != '3' ORDER BY pick_order LIMIT 1",
                              (self.run_id,)).fetchone()[0]
        conn.close()
        store.set_override(1, 'danbooru', banned, 'ban')
        main = db.get_connection()
        folder_id = main.execute("SELECT id FROM collection WHERE name='Artist X'").fetchone()[0]
        # A manually imported image from another provider must survive pruning.
        now = datetime.now(timezone.utc).isoformat()
        manual = main.execute("""INSERT INTO image (sha256, width, height, format, file_size, path, added_at, folder_id)
                                 VALUES ('f'||hex(randomblob(31)), 600, 800, 'jpeg', 1, 'manual.jpg', ?, ?)""", (now, folder_id)).lastrowid
        main.execute("INSERT INTO image_source (image_id, provider, remote_id, fetched_at, metadata) VALUES (?, 'pixiv', '999', ?, '{}')", (manual, now))
        main.commit()
        main.close()

        second_run = store.run_plan(PlannerConfig(min_images=3, max_images=5, character_floor=0))
        second = delivery.create_delivery(second_run, 'Planner', self.root)
        await delivery.run_delivery(second['id'], asyncio.Event(), self.factory, self.root)
        preview = delivery.prune_delivery(second['id'], apply=False)
        self.assertEqual((preview['collections'], preview['images'], preview['removed']), (1, 1, 0))
        applied = delivery.prune_delivery(second['id'], apply=True, library=self.root)
        self.assertEqual(applied['removed'], 1)
        main = db.get_connection()
        remaining = {r[0] for r in main.execute("""SELECT s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id
                                                   WHERE i.folder_id=?""", (folder_id,))}
        main.close()
        self.assertNotIn(banned, remaining)
        self.assertIn('999', remaining)
        self.assertEqual(delivery.prune_delivery(second['id'], apply=False)['images'], 0)


class StyleCheckTests(unittest.TestCase):
    def test_style_outliers_flags_the_odd_image_and_spares_small_artists(self):
        rng = np.random.default_rng(0)
        base = rng.normal(size=32)
        vectors = np.stack([base + rng.normal(scale=0.05, size=32) for _ in range(12)] + [-base])
        distances, scores, flags = style.style_outliers(vectors, threshold=3.5, min_images=8)
        self.assertEqual(flags.tolist(), [False] * 12 + [True])
        self.assertGreater(distances[-1], distances[:-1].max())
        _, _, small = style.style_outliers(vectors[:5].copy(), threshold=3.5, min_images=8)
        self.assertFalse(small.any())

    def test_load_items_maps_frames_and_write_results_respects_locks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            planner_db, main_db = root / 'planner.db', root / 'index.db'
            p = sqlite3.connect(planner_db)
            p.executescript("""CREATE TABLE artist (id INTEGER PRIMARY KEY, display_name TEXT);
                CREATE TABLE delivery (id INTEGER PRIMARY KEY);
                CREATE TABLE delivery_item (delivery_id, site, remote_id, artist_id, folder_id, status);
                CREATE TABLE override (artist_id, site, remote_id, action, created_at, PRIMARY KEY (artist_id, site, remote_id));
                INSERT INTO artist VALUES (1, 'Artist X');
                INSERT INTO delivery VALUES (4);
                INSERT INTO delivery_item VALUES (4, 'danbooru', '10', 1, 7, 'done'), (4, 'danbooru', '11', 1, 7, 'missing'),
                                                 (4, 'danbooru', '12', 1, 7, 'skipped');
                INSERT INTO override VALUES (1, 'danbooru', '12', 'lock', 'x');""")
            p.commit()
            p.close()
            m = sqlite3.connect(main_db)
            m.executescript("""CREATE TABLE image (id INTEGER PRIMARY KEY, path TEXT, folder_id INTEGER);
                CREATE TABLE image_source (image_id, provider, remote_id);
                INSERT INTO image VALUES (1, 'g/x/a.jpg', 7), (2, 'g/x/b.png', 7), (3, 'g/x/c.png', 7), (4, 'g/x/d.jpg', 8);
                INSERT INTO image_source VALUES (1, 'danbooru', '10'), (2, 'danbooru', '12:frame:1'), (3, 'danbooru', '12:frame:2'),
                                                (4, 'danbooru', '10');""")
            m.commit()
            m.close()
            delivery_id, images = style.load_items(root, main_db, planner_db, None)
            self.assertEqual(delivery_id, 4)
            self.assertEqual(sorted((i['remote_id'], i['image_id']) for i in images), [('10', 1), ('12', 2), ('12', 3)])
            results = [{'artist_id': 1, 'site': 'danbooru', 'remote_id': rid, 'distance': 0.5, 'score': 9.0, 'flagged': True}
                       for rid in ('10', '12')]
            self.assertEqual(style.write_results(planner_db, results, {1}, ban=True), 1)
            p = sqlite3.connect(planner_db)
            self.assertEqual(p.execute('SELECT count(*) FROM style_flag').fetchone()[0], 2)
            self.assertEqual(dict(p.execute('SELECT remote_id, action FROM override').fetchall()), {'10': 'ban', '12': 'lock'})
            p.close()


if __name__ == '__main__':
    unittest.main()
