import asyncio
import json
import sqlite3
import unittest
from pathlib import Path

import app.db as db
from app.services import captions
from app.services import planner_curation as curation
from app.services import planner_delivery as delivery
from app.services import planner_quality as quality
from app.services import planner_store as store
from app.services.filters import LocalFilter, LocalQuery, list_images
from app.services.planner_select import PlannerConfig
from app.services.post_dates import YEAR_SQL, post_year, sortable_time
import test_planner
import test_planner_delivery

GELBOORU_2024 = 'Sat Mar 02 13:05:57 -0600 2024'


class PostDateTests(unittest.TestCase):
    def test_years_and_sorting_work_for_iso_and_gelbooru_dates(self):
        self.assertEqual([post_year(v) for v in ('2023-05-01T10:00:00.000-04:00', GELBOORU_2024, '', None, 'garbage')],
                         [2023, 2024, 0, 0, 0])
        conn = sqlite3.connect(':memory:')
        years = [conn.execute(f'SELECT {YEAR_SQL} FROM (SELECT ? AS created_at)', (v,)).fetchone()[0]
                 for v in ('2023-05-01T10:00:00Z', GELBOORU_2024, 'x')]
        conn.close()
        self.assertEqual(years, [2023, 2024, 0])
        ordered = sorted(['Mon Jan 01 00:00:00 -0600 2024', 'Fri Dec 01 00:00:00 -0600 2023', 'Wed Jan 10 00:00:00 -0600 2024'],
                         key=sortable_time)
        self.assertEqual(ordered[0], 'Fri Dec 01 00:00:00 -0600 2023')
        self.assertLess(sortable_time('2023-12-31T23:00:00Z'), sortable_time(GELBOORU_2024))

    def test_posted_at_reads_every_sites_publication_date(self):
        from app.services.post_dates import posted_at
        self.assertEqual(posted_at({'created_at': '2019-04-30T12:00:00.000-04:00'}), '2019-04-30T16:00:00+00:00')  # Danbooru / e621
        self.assertEqual(posted_at({'created_at': GELBOORU_2024}), '2024-03-02T19:05:57+00:00')                  # Gelbooru
        self.assertEqual(posted_at({'date': '2023-01-02 03:04:05'}), '2023-01-02T03:04:05+00:00')                # gallery-dl
        self.assertEqual(posted_at({'published_time': '1700000000'}), '2023-11-14T22:13:20+00:00')               # DeviantArt
        self.assertIsNone(posted_at({'id': 5}))
        self.assertIsNone(posted_at(None))

    def test_newest_posts_window_orders_gelbooru_dates_chronologically(self):
        test_planner.PlannerStoreTests.setUp(self)
        try:
            store.import_artists('site,display_name,query_tag\ngelbooru,g,artist_g\n')
            # Alphabetical order of the raw text would pick Wed/Tue/Thu, not the newest posts.
            dates = {1: 'Wed Jan 10 00:00:00 -0600 2018', 2: 'Tue Feb 06 00:00:00 -0600 2024',
                     3: 'Thu Mar 07 00:00:00 -0600 2024', 4: 'Mon Apr 01 00:00:00 -0600 2024', 5: 'Fri Jun 01 00:00:00 -0600 2018'}
            conn = store.connect()
            rows = [test_planner.post(i, created_at=dates[i]) for i in dates]
            conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                             [tuple({**r, 'site': 'gelbooru', 'artist_id': 1}[c] for c in store.POST_COLUMNS) for r in rows])
            conn.commit()
            conn.close()
            run_id = store.run_plan(PlannerConfig(min_images=2, max_images=10, character_floor=0, newest_posts_per_artist=3))
            conn = store.connect()
            picked = sorted(int(r[0]) for r in conn.execute('SELECT remote_id FROM selection WHERE run_id=?', (run_id,)))
            conn.close()
            self.assertEqual(picked, [2, 3, 4])
        finally:
            test_planner.PlannerStoreTests.tearDown(self)


class HistogramTests(unittest.TestCase):
    def test_shares_are_tie_safe_and_small_buckets_fall_back(self):
        config = quality.QualityConfig(min_bucket=10, low_quality_bottom=20)
        rows = [('danbooru', 2024, 'general', value, 1) for value in range(1, 21)]  # 20 posts scored 1..20
        rows += [('danbooru', 2024, 'explicit', 50, 1)]                             # too few: falls back to 2024
        rows += [('danbooru', 2010, 'general', 0, 30)]                               # all tied at 0
        hist = quality.Histograms(config, rows)
        tag, info = hist.assess('danbooru', 20, 2024, 'general')
        self.assertEqual((tag, info['top'], info['rating']), ('masterpiece', 5.0, 'general'))
        self.assertEqual(hist.assess('danbooru', 19, 2024, 'general')[0], 'best quality')
        self.assertIsNone(hist.assess('danbooru', 10, 2024, 'general')[0])
        self.assertEqual(hist.assess('danbooru', 4, 2024, 'general')[0], 'low quality')
        tag, info = hist.assess('danbooru', 50, 2024, 'explicit')
        self.assertEqual((info['rating'], info['n']), (None, 21))  # the whole 2024 bucket
        self.assertEqual(tag, 'masterpiece')
        # A bucket where everyone ties gets no quality tags at all.
        self.assertIsNone(hist.assess('danbooru', 0, 2010, 'general')[0])
        self.assertEqual(hist.assess('danbooru', None, 2024, 'general'), (None, {'reason': 'no score'}))
        bucket = hist.buckets[('danbooru', 2024, 'general')]
        self.assertEqual((bucket.top_threshold(5), bucket.top_threshold(10), bucket.bottom_threshold(20)), (20, 19, 4))

    def test_config_rejects_overlapping_shares(self):
        with self.assertRaises(ValueError):
            quality.QualityConfig(masterpiece_top=10, best_quality_top=5)
        with self.assertRaises(ValueError):
            quality.QualityConfig(best_quality_top=60, low_quality_bottom=50)


class DeliveredFolderTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_planner_delivery.DeliveryTests.setUp
    tearDown = test_planner_delivery.DeliveryTests.tearDown
    factory = test_planner_delivery.DeliveryTests.factory

    async def delivered(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        main = db.get_connection()
        folders = {r['name']: r['id'] for r in main.execute('SELECT id, name FROM collection')}
        main.close()
        return folders

    def images_by_post(self, folder_id):
        main = db.get_connection()
        try:
            return {r['remote_id']: dict(r) for r in main.execute(
                """SELECT i.id, i.path, i.quality_mark, i.quality_source, i.quality_auto, i.quality_auto_info, s.remote_id
                   FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?""", (folder_id,))}
        finally:
            main.close()


class AutoQualityTests(DeliveredFolderTests):
    async def test_auto_marks_follow_score_percentiles_and_keep_hand_set_marks(self):
        folders = await self.delivered()
        artist_x, beast = folders['Artist X'], folders['beast maker']
        store.import_artists('site,display_name,query_tag\ndanbooru,Artist X,artist_x\ne621,beast maker,beast_maker\ndanbooru,filler,filler\n')
        conn = store.connect()
        filler = conn.execute("SELECT id FROM artist WHERE tag='filler'").fetchone()[0]
        rows = [test_planner_delivery.planner_post(1000 + i, 'danbooru', 'filler', score=i) for i in range(1, 1001)]
        conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                         [tuple({**r, 'artist_id': filler}[c] for c in store.POST_COLUMNS) for r in rows])
        for remote_id, score in {'1': 500, '2': 999, '3': 1, '4': 920, '5': 5, '6': 300}.items():
            conn.execute("UPDATE post SET score=? WHERE site='danbooru' AND remote_id=?", (score, remote_id))
        conn.commit()
        conn.close()
        images = self.images_by_post(artist_x)
        # A hand-set mark survives auto tagging.
        curation.set_marks(artist_x, images['6']['id'], 'masterpiece', None, touched=['quality'])
        curation.set_complete(beast, True)

        config = quality.save_config(quality.QualityConfig(low_quality_bottom=1))
        stats = quality.build_stats(config.metric)
        self.assertEqual(stats['posts'], 1010)
        result = quality.apply(None, config)
        self.assertEqual((result['folders'], result['locked_folders'], result['kept_manual']), (1, 1, 1))
        images = self.images_by_post(artist_x)
        marks = {rid: (image['quality_mark'], image['quality_source']) for rid, image in images.items()}
        # Delivered posts: 2 (score 999), 4 (920), 5 (5) and 6 (300, marked by hand).
        self.assertEqual(marks, {'2': ('masterpiece', 'auto'), '4': ('best quality', 'auto'), '5': ('low quality', 'auto'),
                                 '6': ('masterpiece', 'manual')})
        self.assertEqual(images['6']['quality_auto'], None)
        info = json.loads(images['2']['quality_auto_info'])
        self.assertEqual((info['site'], info['year'], info['rating'], info['value']), ('danbooru', 2025, 'general', 999))
        self.assertLess(info['top'], 1)

        # Thresholds read like a percentile table.
        table = quality.thresholds(config)
        danbooru = next(site for site in table['sites'] if site['site'] == 'danbooru')
        self.assertEqual(danbooru['overall']['n'], 1006)
        self.assertEqual(danbooru['overall']['masterpiece'], 952)
        self.assertEqual(danbooru['years'][0]['ratings']['general']['n'], 1006)

        # Hand marks, auto/manual filters and "use auto".
        ids = lambda **f: {i['id'] for i in list_images(artist_x, LocalQuery(filters=LocalFilter(**f)))['items']}
        self.assertEqual(ids(quality='manual'), {images['6']['id']})
        self.assertIn(images['2']['id'], ids(quality='auto'))
        row = curation.set_marks(artist_x, images['6']['id'], None, None, use_auto=True)
        self.assertEqual((row['quality_mark'], row['quality_source']), (None, 'auto'))
        row = curation.set_marks(artist_x, images['2']['id'], 'masterpiece', 'aesthetic', touched=['aesthetic'])
        self.assertEqual(row['quality_source'], 'auto')  # only the aesthetic scale was touched
        row = curation.set_marks(artist_x, images['2']['id'], None, None, touched=['quality', 'aesthetic'])
        self.assertEqual((row['quality_mark'], row['quality_source']), (None, 'manual'))
        # Re-running auto keeps an explicit Normal.
        quality.apply([artist_x], config)
        self.assertEqual(self.images_by_post(artist_x)['2']['quality_mark'], None)
        self.assertEqual(curation.folder_context(artist_x)['marks']['auto'], 2)


class CaptionTests(DeliveredFolderTests):
    async def test_caption_files_are_read_edited_and_follow_their_image(self):
        folders = await self.delivered()
        folder_id = folders['Artist X']
        images = list(self.images_by_post(folder_id).values())
        image, other = images[0], images[1]
        image_path = self.root / 'images' / image['path']
        caption_path = image_path.with_name(image_path.stem + '_nl.txt')

        empty = captions.read_caption(image['id'])
        self.assertEqual((empty['exists'], empty['filename'], empty['version']), (False, caption_path.name, None))
        caption_path.write_text('A girl in a red coat.', encoding='utf-8')
        loaded = captions.read_caption(image['id'])
        self.assertEqual(loaded['text'], 'A girl in a red coat.')
        saved = captions.write_caption(image['id'], 'A girl in a red coat,\r\nwalking.', loaded['version'])
        self.assertEqual(caption_path.read_bytes(), b'A girl in a red coat,\nwalking.')
        backups = list((self.root / '.trash' / 'captions').rglob('*'))
        self.assertTrue(any(p.is_file() and p.read_text(encoding='utf-8') == 'A girl in a red coat.' for p in backups))
        # A captioning run rewrote the file meanwhile: the stale editor must not overwrite it.
        caption_path.write_text('Rewritten by the captioner, longer text.', encoding='utf-8')
        with self.assertRaises(captions.CaptionConflict):
            captions.write_caption(image['id'], 'stale', saved['version'])
        with self.assertRaises(captions.CaptionConflict):
            captions.write_caption(other['id'], 'new', 'not-missing')
        created = captions.write_caption(other['id'], 'Created in the viewer.', None)
        self.assertTrue(created['exists'])

        listed = {i['id']: i['has_caption'] for i in list_images(folder_id, LocalQuery())['items']}
        self.assertTrue(listed[image['id']] and listed[other['id']])
        has = {i['id'] for i in list_images(folder_id, LocalQuery(filters=LocalFilter(caption='has')))['items']}
        missing = {i['id'] for i in list_images(folder_id, LocalQuery(filters=LocalFilter(caption='missing')))['items']}
        self.assertEqual(has, {image['id'], other['id']})
        self.assertFalse(has & missing)

        # Removal moves the caption to recovery; recovery brings it back.
        from app.services.collections import CollectionService
        service = CollectionService()
        service.remove_images(folder_id, [image['id']], self.root, 'cd' * 16)
        self.assertFalse(caption_path.exists())
        self.assertTrue((self.root / '.trash' / ('cd' * 16) / 'images' / Path(image['path']).with_name(caption_path.name)).is_file())
        service.restore_images(folder_id, 'cd' * 16, self.root)
        self.assertEqual(caption_path.read_text(encoding='utf-8'), 'Rewritten by the captioner, longer text.')

        deleted = captions.delete_caption(other['id'], captions.read_caption(other['id'])['version'])
        self.assertFalse(deleted['exists'])

        # Another caption ending.
        with self.assertRaises(ValueError):
            captions.set_suffix('.txt')
        with self.assertRaises(ValueError):
            captions.set_suffix('../x.txt')
        captions.set_suffix('.caption')
        self.assertEqual(captions.read_caption(image['id'])['filename'], image_path.stem + '.caption')

    async def test_dedup_keeps_the_removed_images_caption(self):
        folders = await self.delivered()
        images = list(self.images_by_post(folders['Artist X']).values())
        loser, winner = images[0], images[1]
        root = self.root / 'images'
        loser_caption = root / captions.caption_relative(loser['path'], '_nl.txt')
        winner_caption = root / captions.caption_relative(winner['path'], '_nl.txt')
        loser_caption.write_text('caption of the removed copy', encoding='utf-8')
        from app.services.dedup import DedupService
        DedupService(self.root)._keep_caption(loser, winner)
        self.assertEqual(winner_caption.read_text(encoding='utf-8'), 'caption of the removed copy')
        self.assertFalse(loser_caption.exists())

    async def test_exports_copy_caption_files_and_routes_report_conflicts(self):
        from fastapi import HTTPException
        from app.routes import images as image_routes
        from app.services.qa import DatasetQAService
        folders = await self.delivered()
        folder_id = folders['Artist X']
        image = next(iter(self.images_by_post(folder_id).values()))
        saved = image_routes.save_caption(image['id'], image_routes.CaptionWrite(text='An exported caption.'))
        with self.assertRaises(HTTPException) as caught:
            image_routes.save_caption(image['id'], image_routes.CaptionWrite(text='stale'))
        self.assertEqual(caught.exception.status_code, 409)
        export = DatasetQAService(self.root).create_export(folder_id, 'copy')
        manifest = json.loads(Path(export['manifest_path']).read_text(encoding='utf-8'))
        item = next(i for i in manifest['items'] if i['image_id'] == image['id'])
        self.assertEqual(item['caption_filename'], Path(item['filename']).stem + '_nl.txt')
        self.assertEqual((Path(export['output_path']) / item['caption_filename']).read_text(encoding='utf-8'), saved['text'])
        self.assertTrue(all(i['caption_filename'] is None for i in manifest['items'] if i['image_id'] != image['id']))


class PostedDateTests(DeliveredFolderTests):
    async def test_image_details_include_the_original_posting_date(self):
        from app.services.images import ImageService
        folders = await self.delivered()
        image = next(iter(self.images_by_post(folders['Artist X']).values()))
        main = db.get_connection()
        main.execute('UPDATE image_source SET metadata=? WHERE image_id=?', (json.dumps({'id': 1, 'created_at': GELBOORU_2024}), image['id']))
        main.commit()
        main.close()
        details = ImageService(self.root).get_image_by_id(image['id'])
        self.assertEqual((details['posted_at'], details['posted_on']), ('2024-03-02T19:05:57+00:00', 'danbooru'))
        self.assertEqual(details['sources'][0]['posted_at'], '2024-03-02T19:05:57+00:00')
        self.assertNotEqual(details['posted_at'][:10], details['added_at'][:10])


class QualityRouteTests(DeliveredFolderTests):
    async def test_apply_job_builds_statistics_then_assigns_marks(self):
        from fastapi import HTTPException
        from app.routes import planner as routes
        folders = await self.delivered()
        routes.save_quality_config(quality.QualityConfig(min_bucket=1))
        started = await routes.apply_quality(routes.QualityApplyRequest(folder_ids=[folders['Artist X']]))
        self.assertEqual(started['status'], 'running')
        with self.assertRaises(HTTPException) as caught:
            await routes.apply_quality(routes.QualityApplyRequest())
        self.assertEqual(caught.exception.status_code, 409)
        await routes._quality_task
        job = routes.quality_job()
        self.assertEqual(job['status'], 'completed', job.get('error'))
        self.assertEqual(job['stats']['posts'], 10)
        self.assertEqual((job['result']['folders'], job['result']['images']), (1, 4))
        status = routes.quality_status()
        self.assertEqual((status['config']['min_bucket'], status['stats']['posts']), (1, 10))
        self.assertTrue(routes.quality_thresholds()['sites'])


if __name__ == '__main__':
    unittest.main()
