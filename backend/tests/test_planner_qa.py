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


    async def test_removed_images_become_bans_and_layout_repeats_follow_what_is_left(self):
        import json
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        main = db.get_connection()
        folder_id = main.execute("SELECT id FROM collection WHERE name='Artist X'").fetchone()[0]
        rows = main.execute("""SELECT i.id, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id
                               WHERE i.folder_id=? ORDER BY s.remote_id""", (folder_id,)).fetchall()
        main.close()
        removed_image, removed_post = rows[0]['id'], rows[0]['remote_id']
        from app.services.collections import CollectionService
        CollectionService().remove_images(folder_id, [removed_image], self.root, 'ab' * 16)

        layout = delivery.export_training_layout(job['id'])
        artist_x = next(f for f in json.loads((layout / 'folders.json').read_text(encoding='utf-8')) if f['artist'] == 'Artist X')
        self.assertEqual(artist_x['images'], len(rows) - 1)
        self.assertEqual(artist_x['repeats'], min(10, round(200 / (len(rows) - 1))))

        self.assertEqual(delivery.ban_removed_images(), 1)
        self.assertEqual(delivery.ban_removed_images(), 0)  # already banned
        second_run = store.run_plan(PlannerConfig(min_images=3, max_images=5, character_floor=0))
        conn = store.connect()
        picked = {r[0] for r in conn.execute("SELECT remote_id FROM selection WHERE run_id=? AND site='danbooru'", (second_run,))}
        conn.close()
        self.assertNotIn(removed_post, picked)


class CurationTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_planner_delivery.DeliveryTests.setUp
    tearDown = test_planner_delivery.DeliveryTests.tearDown
    factory = test_planner_delivery.DeliveryTests.factory

    async def _delivered_folder(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        main = db.get_connection()
        folder_id = main.execute("SELECT id FROM collection WHERE name='Artist X'").fetchone()[0]
        main.close()
        return folder_id

    def _picked(self, run_id):
        conn = store.connect()
        try:
            return {r[0] for r in conn.execute("SELECT remote_id FROM selection WHERE run_id=? AND site='danbooru'", (run_id,))}
        finally:
            conn.close()

    async def test_caption_check_flags_and_sets_aside_captions(self):
        from app.services import caption_check
        folder_id = await self._delivered_folder()
        main = db.get_connection()
        paths = [r[0] for r in main.execute('SELECT path FROM image WHERE folder_id=? ORDER BY id', (folder_id,))]
        main.close()
        root = db.LIBRARY_PATH / 'images'
        refusal = (root / paths[0]).with_name(Path(paths[0]).stem + '_nl.txt')
        refusal.write_text("I'm sorry, but I can't help with describing this image.", encoding='utf-8')
        result = caption_check.run(min_words=0, max_words=1000)
        self.assertTrue(result['images'] >= len(paths) and result['captioned'] == 1)
        self.assertIn('refusal', result['counts'])
        self.assertEqual(caption_check.flagged_items('refusal')[0]['path'], paths[0])
        self.assertEqual(caption_check.set_aside(['refusal']), {'set_aside': 1})
        self.assertFalse(refusal.exists())
        self.assertTrue(refusal.with_name(refusal.name + '.flagged').exists())

    async def test_accept_all_and_reopen_all(self):
        from app.services import planner_curation as curation
        folder_id = await self._delivered_folder()
        accepted = curation.set_complete_all(True)
        self.assertTrue(accepted['folders'] >= 1 and accepted['changed'] == accepted['folders'])
        self.assertTrue(curation.folder_context(folder_id)['completed_at'])
        self.assertEqual(curation.set_complete_all(True)['changed'], 0)  # already accepted
        self.assertEqual(curation.set_complete_all(False)['changed'], accepted['folders'])
        self.assertIsNone(curation.folder_context(folder_id)['completed_at'])

    async def test_candidates_accept_and_complete_freeze_the_selection(self):
        from app.services import planner_curation as curation
        folder_id = await self._delivered_folder()
        context = curation.folder_context(folder_id)
        self.assertEqual((context['tag'], context['target'], context['images'], context['completed_at']), ('artist_x', 5, 4, None))
        present = {r for r in self._picked(self.run_id) if r != '3'}
        listed = curation.candidates(folder_id)
        # Everything harvested that is not in the folder: the unselected post and deleted post 3.
        self.assertEqual({i['remote_id'] for i in listed['items']}, {str(i) for i in range(1, 7)} - present)
        extra = next(i['remote_id'] for i in listed['items'] if i['remote_id'] != '3')
        store.set_override(1, 'danbooru', extra, 'ban')
        self.assertNotIn(extra, {i['remote_id'] for i in curation.candidates(folder_id)['items']})
        self.assertIn(extra, {i['remote_id'] for i in curation.candidates(folder_id, include_banned=True)['items']})

        result = curation.accept_posts(folder_id, [('danbooru', extra), ('danbooru', 'nope')])
        self.assertEqual(result, {'accepted': 1, 'by_site': {'danbooru': [extra]}})
        run = store.run_plan(PlannerConfig(min_images=3, max_images=5, character_floor=0))
        self.assertIn(extra, self._picked(run))

        context = curation.set_complete(folder_id, True)
        self.assertTrue(context['completed_at'])
        main = db.get_connection()
        statuses = {r[0] for r in main.execute('SELECT review_status FROM image WHERE folder_id=?', (folder_id,))}
        main.close()
        self.assertEqual(statuses, {'accepted'})
        with self.assertRaises(ValueError):
            curation.accept_posts(folder_id, [('danbooru', '3')])
        # A completed collection selects exactly its images, even with a larger target.
        run = store.run_plan(PlannerConfig(min_images=3, max_images=50, character_floor=0))
        self.assertEqual(self._picked(run), present)

        curation.set_complete(folder_id, False)
        main = db.get_connection()
        statuses = {r[0] for r in main.execute('SELECT review_status FROM image WHERE folder_id=?', (folder_id,))}
        main.close()
        self.assertEqual(statuses, {'pending'})
        # After reopening, removing an image still bans its post.
        main = db.get_connection()
        image, post = main.execute("""SELECT i.id, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id
                                     WHERE i.folder_id=? LIMIT 1""", (folder_id,)).fetchone()
        main.close()
        from app.services.collections import CollectionService
        CollectionService().remove_images(folder_id, [image], self.root, 'ab' * 16)
        self.assertEqual(delivery.ban_removed_images(), 1)
        self.assertNotIn(post, self._picked(store.run_plan(PlannerConfig(min_images=3, max_images=5, character_floor=0))))

    async def test_removing_an_accepted_image_turns_its_lock_into_a_ban(self):
        from app.services import planner_curation as curation
        folder_id = await self._delivered_folder()
        extra = next(i['remote_id'] for i in curation.candidates(folder_id)['items'] if i['remote_id'] != '3')
        curation.accept_posts(folder_id, [('danbooru', extra)])
        # Not imported yet: nothing to ban.
        self.assertEqual(delivery.ban_removed_images(), 0)
        main = db.get_connection()
        batch = main.execute("INSERT INTO import_batch (collection_id, provider, status, created_at) VALUES (?, 'danbooru', 'completed', 'x')",
                             (folder_id,)).lastrowid
        image = main.execute('SELECT id FROM image WHERE folder_id=? LIMIT 1', (folder_id,)).fetchone()[0]
        main.execute("INSERT INTO import_item (batch_id, remote_id, status, image_id) VALUES (?, ?, 'downloaded', ?)", (batch, extra, image))
        main.commit()
        main.close()
        # Imported, then removed by hand (its image is not in the folder).
        self.assertEqual(delivery.ban_removed_images(), 1)
        conn = store.connect()
        action = conn.execute("SELECT action FROM override WHERE artist_id=1 AND site='danbooru' AND remote_id=?", (extra,)).fetchone()[0]
        conn.close()
        self.assertEqual(action, 'ban')
        self.assertEqual(delivery.ban_removed_images(), 0)

    async def test_routes_reject_other_folders_and_previews_are_resized_and_cached(self):
        import io
        from unittest import mock
        import httpx
        from fastapi import HTTPException
        from PIL import Image
        from app.routes import planner as routes
        folder_id = await self._delivered_folder()
        with self.assertRaises(HTTPException) as caught:
            routes.folder_context(folder_id + 100)
        self.assertEqual(caught.exception.status_code, 404)
        routes.complete_folder(folder_id, routes.CompleteRequest(complete=True))
        with self.assertRaises(HTTPException) as caught:
            routes.accept_candidates(folder_id, routes.AcceptRequest(posts=[{'site': 'danbooru', 'remote_id': '3'}]))
        self.assertEqual(caught.exception.status_code, 409)

        self.assertEqual(routes._variant_urls('danbooru', 'https://cdn.donmai.us/180x180/ab/cd/x.jpg', 'https://cdn.donmai.us/original/x.png', 'view')[0],
                         'https://cdn.donmai.us/720x720/ab/cd/x.jpg')
        self.assertEqual(routes._variant_urls('e621', 'https://static1.e621.net/data/preview/ab/cd/x.jpg', None, 'grid'),
                         ['https://static1.e621.net/data/sample/ab/cd/x.jpg', 'https://static1.e621.net/data/preview/ab/cd/x.jpg'])
        self.assertEqual(routes._variant_urls('gelbooru', 'https://img3.gelbooru.com/thumbnails/ab/cd/thumbnail_x.jpg', None, 'grid')[0],
                         'https://img3.gelbooru.com/samples/ab/cd/sample_x.jpg')

        conn = store.connect()
        conn.execute("UPDATE post SET preview_url='https://cdn.donmai.us/180x180/a/b/2.jpg', file_url='https://cdn.donmai.us/original/a/b/2.png' WHERE site='danbooru' AND remote_id='2'")
        conn.commit()
        conn.close()
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if '/720x720/' in str(request.url):
                return httpx.Response(404)
            out = io.BytesIO()
            Image.new('RGB', (3000, 2000), (10, 20, 30)).save(out, 'PNG')
            return httpx.Response(200, content=out.getvalue(), headers={'content-type': 'image/png'})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with mock.patch.object(routes, '_client', lambda: client):
            response = await routes.post_preview(1, 'danbooru', '2', size='view')
            again = await routes.post_preview(1, 'danbooru', '2', size='view')
        await client.aclose()
        self.assertEqual(requested, ['https://cdn.donmai.us/720x720/a/b/2.jpg', 'https://cdn.donmai.us/original/a/b/2.png'])
        self.assertEqual(response.path, again.path)
        with Image.open(response.path) as image:
            self.assertEqual(image.size, (1024, 683))

    async def test_quality_marks_reach_the_sidecar_only_when_the_folder_is_accepted(self):
        from app.services import planner_curation as curation
        from app.services.filters import LocalFilter, LocalQuery, list_images
        from app.services.tags import TagService
        folder_id = await self._delivered_folder()
        main = db.get_connection()
        first, second, third = [r[0] for r in main.execute('SELECT id FROM image WHERE folder_id=? ORDER BY id LIMIT 3', (folder_id,))]
        path = main.execute('SELECT path FROM image WHERE id=?', (first,)).fetchone()[0]
        main.close()
        sidecar = self.root / 'images' / Path(path).with_suffix('.txt')
        before = sidecar.read_text(encoding='utf-8')

        def ids(**filters):
            return {i['id'] for i in list_images(folder_id, LocalQuery(filters=LocalFilter(**filters)))['items']}

        curation.set_marks(folder_id, first, 'masterpiece', 'very aesthetic')
        curation.set_marks(folder_id, second, 'low quality', None)
        curation.mark_viewed(folder_id, third)
        marks = curation.folder_context(folder_id)['marks']
        self.assertEqual((marks['viewed'], marks['masterpiece'], marks['very aesthetic'], marks['low quality']), (3, 1, 1, 1))
        self.assertEqual(ids(quality='masterpiece'), {first})
        self.assertEqual(ids(quality='very aesthetic'), {first})
        self.assertIn(third, ids(quality='normal'))
        self.assertNotIn(first, ids(quality='normal'))
        self.assertNotIn(third, ids(quality='unviewed'))
        with self.assertRaises(ValueError):
            curation.set_marks(folder_id, first, 'aesthetic', None)
        # Not written before the folder is accepted.
        self.assertEqual(sidecar.read_text(encoding='utf-8'), before)
        self.assertEqual(ids(required_tags=['masterpiece']), set())

        curation.set_complete(folder_id, True)
        self.assertEqual(sidecar.read_text(encoding='utf-8'), before + ', masterpiece, very aesthetic')
        self.assertEqual(ids(required_tags=['masterpiece']), {first})
        with self.assertRaises(ValueError):
            curation.set_marks(folder_id, first, None, None)
        # Editing the ground truth keeps the managed tags last and never duplicates them.
        tags = TagService(self.root)
        edited = tags.get_ground_truth(first)['tags']
        tags.replace_ground_truth(first, ['extra tag', *edited])
        self.assertTrue(sidecar.read_text(encoding='utf-8').endswith('masterpiece, very aesthetic'))
        self.assertEqual(sidecar.read_text(encoding='utf-8').count('masterpiece'), 1)

        # Reopened: marks change, the sidecar keeps the committed tags until accepted again.
        curation.set_complete(folder_id, False)
        curation.set_marks(folder_id, first, 'best quality', None)
        self.assertTrue(sidecar.read_text(encoding='utf-8').endswith('masterpiece, very aesthetic'))
        curation.set_complete(folder_id, True)
        text = sidecar.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('extra tag') or 'extra tag' in text)
        self.assertTrue(text.endswith(', best quality'))
        self.assertNotIn('masterpiece', text)


class RecencyTests(unittest.TestCase):
    setUp = test_planner.PlannerStoreTests.setUp
    tearDown = test_planner.PlannerStoreTests.tearDown

    def test_newest_posts_window_limits_candidates_to_recent_work(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,a,artist_a\n')
        conn = store.connect()
        rows = [test_planner.post(i, created_at=f'20{10 + i:02d}-01-01T00:00:00') for i in range(1, 11)]
        conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                         [tuple({**r, 'artist_id': 1}[c] for c in store.POST_COLUMNS) for r in rows])
        conn.commit()
        conn.close()
        run_id = store.run_plan(PlannerConfig(min_images=2, max_images=10, character_floor=0, newest_posts_per_artist=4))
        conn = store.connect()
        picked = sorted(int(r[0]) for r in conn.execute('SELECT remote_id FROM selection WHERE run_id=?', (run_id,)))
        conn.close()
        self.assertEqual(picked, [7, 8, 9, 10])

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
