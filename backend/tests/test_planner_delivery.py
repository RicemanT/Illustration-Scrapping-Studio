import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import app.db as db
from app.models import RemotePost
from app.services import planner_delivery as delivery
from app.services import planner_store as store
from app.services.planner_select import PlannerConfig


def planner_post(remote_id, site, artist_tag, **values):
    row = {'site': site, 'remote_id': str(remote_id), 'md5': f'{site}{remote_id:032d}'[-32:], 'width': 1200, 'height': 1600,
           'ext': 'jpg', 'rating': 'general', 'score': 1, 'fav_count': remote_id, 'parent_id': None, 'has_children': 0,
           'created_at': '2025-01-01', 'artists': artist_tag, 'characters': '', 'copyrights': '', 'species': '',
           'general': f'1girl tag_{remote_id % 5}', 'meta': '', 'file_url': f'https://example.test/{remote_id}.jpg',
           'preview_url': None}
    row.update(values)
    return row


class FakeSite:
    """Answers id: lookups and serves real JPEGs; post 3 was deleted upstream."""

    deleted = {'3'}

    def __init__(self, site):
        self.site = site
        self.lookups = []

    def post(self, remote_id):
        return RemotePost(provider=self.site, remote_id=remote_id, remote_url=f'https://example.test/posts/{remote_id}',
                          image_url=f'https://example.test/{remote_id}.jpg', preview_url=None, width=600, height=800,
                          format='jpg', md5=None, tags={'artist': ['artist_x'], 'general': ['1girl', f'tag_{remote_id}']},
                          rating='general', score=1, created_at='2025-01-01', raw_metadata={'id': remote_id})

    async def search(self, query, cursor, limit, sort='latest'):
        ids = query.removeprefix('id:').split(',')
        self.lookups.append(ids)
        return [self.post(remote_id) for remote_id in ids if remote_id not in self.deleted], None

    async def download_image(self, post, dest_path, progress=None):
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        shade = int(post.remote_id) * 9 % 255
        Image.new('RGB', (600, 800), (shade, 40, 200 - shade % 150)).save(dest_path, 'JPEG', quality=95)

    async def close(self):
        pass


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = (db.DB_PATH, db.LIBRARY_PATH, os.environ.get('ARTIST_PLANNER_PATH'))
        db.DB_PATH = self.root / 'index.db'
        db.LIBRARY_PATH = self.root
        os.environ['ARTIST_PLANNER_PATH'] = str(self.root / 'planner')
        db.init_db()
        store.import_artists('site,display_name,query_tag\ndanbooru,Artist X,artist_x\ne621,beast maker,beast_maker\n')
        conn = store.connect()
        rows = [planner_post(i, 'danbooru', 'artist_x') for i in range(1, 7)] + \
               [planner_post(i, 'e621', 'beast_maker') for i in range(1, 5)]
        conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                         [tuple({**r, 'artist_id': 1 if r['site'] == 'danbooru' else 2}[c] for c in store.POST_COLUMNS) for r in rows])
        conn.commit()
        conn.close()
        self.run_id = store.run_plan(PlannerConfig(min_images=3, max_images=5, character_floor=0))
        self.sites = {}

    def tearDown(self):
        db.DB_PATH, db.LIBRARY_PATH, planner_path = self.old
        if planner_path is None:
            os.environ.pop('ARTIST_PLANNER_PATH', None)
        else:
            os.environ['ARTIST_PLANNER_PATH'] = planner_path
        self.temp.cleanup()

    def factory(self, site):
        self.sites[site] = FakeSite(site)
        return self.sites[site]

    async def test_delivery_creates_protected_folders_and_imports_selected_posts(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        result = delivery.get_delivery(job['id'])
        self.assertEqual(result['status'], 'completed', result.get('error'))
        main = db.get_connection()
        groups = {r['name']: r['provider'] for r in main.execute('SELECT name, provider FROM artist_group')}
        self.assertEqual(groups, {'Planner danbooru': 'danbooru', 'Planner e621': 'e621'})
        folders = main.execute("""SELECT c.id, c.name, s.query_override, (SELECT count(*) FROM image i WHERE i.folder_id=c.id) images,
                                         EXISTS(SELECT 1 FROM group_blocked_folder b WHERE b.folder_id=c.id) protected
                                  FROM collection c JOIN collection_source s ON s.collection_id=c.id ORDER BY c.name""").fetchall()
        main.close()
        self.assertEqual([(f['name'], f['query_override'], f['protected']) for f in folders],
                         [('Artist X', 'artist_x', 1), ('beast maker', 'beast_maker', 1)])
        conn = store.connect()
        selected = {site: n for site, n in conn.execute('SELECT site, count(*) FROM selection WHERE run_id=? GROUP BY site', (self.run_id,))}
        conn.close()
        # Post 3 was deleted upstream on both sites.
        self.assertEqual([f['images'] for f in folders], [selected['danbooru'] - 1, selected['e621'] - 1])
        counts = result['counts']
        self.assertEqual(counts['danbooru:missing'], 1)
        self.assertEqual(counts['danbooru:done'], selected['danbooru'] - 1)
        self.assertEqual(len(self.sites['danbooru'].lookups), 1)  # one batched metadata request
        sidecars = sorted((self.root / 'images').rglob('*.txt'))
        self.assertTrue(sidecars)
        self.assertIn('artist x', sidecars[0].read_text(encoding='utf-8').lower())

        layout = delivery.export_training_layout(job['id'])
        folders_json = json.loads((layout / 'folders.json').read_text(encoding='utf-8'))
        self.assertEqual({f['artist']: f['repeats'] for f in folders_json}, {'Artist X': 10, 'beast maker': 10})
        toml = (layout / 'subsets.toml').read_text(encoding='utf-8')
        self.assertEqual(toml.splitlines().count('[[subsets]]'), 2)
        self.assertIn('num_repeats = 10', toml)
        self.assertFalse((layout / 'dataset.toml').exists())
        # paths are relative to the layout folder (the dataset root), as <group>/<artist>
        import csv, tomllib
        with (layout / 'folders.csv').open(encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(list(rows[0])[:2], ['path', 'num_repeats'])
        self.assertTrue(all(row['path'].count('/') == 1 and not row['path'].startswith('/') for row in rows))
        for stage in ('512', '1024'):
            config = tomllib.loads((layout / f'mageflow-{stage}.toml').read_text(encoding='utf-8'))
            self.assertEqual(config['dataset']['subsets_file'], '@DATA_DIR@/folders.csv')
            self.assertEqual(config['dataset']['resolution'], int(stage))
            self.assertEqual(config['dataset']['caption']['attribution_patterns'], ['^drawn by\\s'])
            self.assertTrue(config['sampling']['prompts'][0].lower().startswith('drawn by'))

        # A second delivery of the same run reuses the folders and downloads nothing new.
        again = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(again['id'], asyncio.Event(), self.factory, self.root)
        again = delivery.get_delivery(again['id'])
        self.assertNotIn('danbooru:done', again['counts'])
        self.assertEqual(again['counts']['danbooru:skipped'], selected['danbooru'] - 1)
        # Export training layout can pick any downloaded run; each is listed with its latest download.
        runs = store.status()['delivered_runs']
        self.assertEqual([(r['run_id'], r['delivery_id'], r['deliveries'], r['group_prefix']) for r in runs],
                         [(self.run_id, again['id'], 2, 'Planner')])
        main = db.get_connection()
        self.assertEqual(main.execute('SELECT count(*) FROM collection').fetchone()[0], 2)
        main.close()
        # A later batch under another prefix keeps these artists in their folders: no duplicates, no new groups.
        batch = delivery.create_delivery(self.run_id, 'batch1', self.root)
        self.assertEqual(batch['progress']['groups'], {})
        main = db.get_connection()
        self.assertEqual(main.execute('SELECT count(*) FROM collection').fetchone()[0], 2)
        main.close()

    async def test_problems_list_reasons_and_completed_downloads_can_retry_errors(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        conn = store.connect()
        conn.execute("UPDATE delivery_item SET status='error', error='HTTP 502' WHERE delivery_id=? AND site='e621' AND remote_id='1'", (job['id'],))
        conn.commit()
        conn.close()
        items = delivery.problems(job['id'])['items']
        self.assertEqual([(i['status'], i['site'], i['remote_id']) for i in items][:2], [('error', 'e621', '1'), ('missing', 'danbooru', '3')])
        self.assertEqual(items[0]['reason'], 'HTTP 502')
        self.assertTrue(items[0]['url'].endswith('/posts/1'))
        delivery.resume_delivery(job['id'])
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        counts = delivery.get_delivery(job['id'])['counts']
        self.assertNotIn('e621:error', counts)
        self.assertEqual(counts['e621:skipped'], 1)  # the image was already in the collection

    async def test_stop_leaves_pending_items_for_resume(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        stop = asyncio.Event()
        stop.set()
        await delivery.run_delivery(job['id'], stop, self.factory, self.root)
        stopped = delivery.get_delivery(job['id'])
        self.assertEqual(stopped['status'], 'canceled')
        self.assertTrue(all(key.endswith(':pending') for key in stopped['counts']))
        delivery.resume_delivery(job['id'])
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        self.assertEqual(delivery.get_delivery(job['id'])['status'], 'completed')

    def test_group_prefix_conflict_with_other_provider_is_refused(self):
        from app.services.groups import create_group
        create_group('Planner danbooru', 'e621', self.root)
        with self.assertRaises(ValueError):
            delivery.create_delivery(self.run_id, 'Planner', self.root)

    def test_only_completed_runs_can_be_delivered(self):
        conn = store.connect()
        conn.execute("UPDATE run SET status='failed' WHERE id=?", (self.run_id,))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError):
            delivery.create_delivery(self.run_id, 'Planner', self.root)


if __name__ == '__main__':
    unittest.main()
