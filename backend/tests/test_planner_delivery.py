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

        # Character and copyright tags go to protected_tags.txt, with hand edits applied.
        main = db.get_connection()
        image_id, source_id = main.execute(
            "SELECT it.image_id, it.source_id FROM image_tag it ORDER BY it.image_id LIMIT 1").fetchone()
        main.executemany("INSERT INTO image_tag (image_id, source_id, category, tag) VALUES (?, ?, ?, ?)",
                         [(image_id, source_id, 'character', 'hatsune_miku'), (image_id, source_id, 'copyright', 'vocaloid'),
                          (image_id, source_id, 'character', 'wrong_character')])
        main.executemany("INSERT INTO image_tag_override (image_id, tag, action, category, updated_at) VALUES (?, ?, ?, ?, '2026-01-01')",
                         [(image_id, 'wrong character', 'remove', 'general'), (image_id, 'Kagamine Rin', 'add', 'character')])
        main.commit()
        main.close()

        layout = delivery.export_training_layout(job['id'])
        protected = [line.split('#', 1)[0].strip() for line in (layout / 'protected_tags.txt').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([tag for tag in protected if tag], ['hatsune miku', 'kagamine rin', 'vocaloid'])
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
        # MOSS-Image configs for the pilot's three stages: the LR halves per stage, each continues from the last.
        self.assertEqual(sorted(p.name for p in layout.glob('*.toml')), ['moss-1024.toml', 'moss-512.toml', 'moss-768.toml', 'subsets.toml'])
        configs = {stage: tomllib.loads((layout / f'moss-{stage}.toml').read_text(encoding='utf-8')) for stage in ('512', '768', '1024')}
        for stage, lr, start in (('512', 4e-5, None), ('768', 2e-5, '@STAGE_512_CHECKPOINT@'), ('1024', 1e-5, '@STAGE_768_CHECKPOINT@')):
            config = configs[stage]
            self.assertEqual(config['train']['model_family'], 'moss_image')
            self.assertEqual(config['train'].get('transformer_path'), start)
            self.assertEqual((config['train']['attention_backend'], config['train']['pack_resolutions']), ('torch_varlen', True))
            self.assertEqual(config['dataset']['subsets_file'], '@DATA_DIR@/folders.csv')
            self.assertEqual(config['dataset']['resolution'], int(stage))
            self.assertEqual([s.get('end_lr', s.get('lr', s.get('max_val'))) for s in config['schedule']['stages']], [lr] * 3)
            caption = config['dataset']['caption']
            self.assertEqual(caption['attribution_patterns'], ['^drawn by\\s'])
            self.assertEqual(caption['protected_tags_file'], '@DATA_DIR@/protected_tags.txt')
            self.assertEqual((caption['layout_panels_as_objects'], caption['layout_short_captions']), (True, 0.5))
            self.assertTrue(config['sampling']['prompts'][0].lower().startswith('drawn by'))
        # Stage lengths: about 4500 / 2600 / 900 views for the median artist (images x repeats per epoch).
        per_epoch = sorted(f['images'] * f['repeats'] for f in folders_json)[len(folders_json) // 2]
        self.assertEqual([configs[s]['train']['epochs'] for s in ('512', '768', '1024')],
                         [max(1, round(v / per_epoch)) for v in (4500, 2600, 900)])
        self.assertEqual(delivery._attribution_patterns(['@{artist}']), ['^@'])
        self.assertEqual(delivery._attribution_patterns(['Drawn by {artist}', '@{artist}']), ['^@', '^drawn by\\s'])
        # No watermark boxes yet: no masks, an empty mask_path column, masked training configured but idle.
        self.assertFalse((layout / 'masks').exists())
        self.assertTrue(all(row['mask_path'] == '' for row in rows))
        self.assertIs(configs['512']['dataset']['use_masks'], True)

        # A watermark box on one image: its folder gets a mask for every image, black only over the box.
        main = db.get_connection()
        folder_id, = main.execute('SELECT folder_id FROM image WHERE id=?', (image_id,)).fetchone()
        images = main.execute('SELECT id, path, width, height FROM image WHERE folder_id=? ORDER BY id', (folder_id,)).fetchall()
        slug = main.execute('SELECT slug FROM collection WHERE id=?', (folder_id,)).fetchone()[0].replace('\\', '/').strip('/')
        main.close()
        first = self.root / 'images' / images[0][1]
        first.with_name(first.stem + '_moss.json').write_text(json.dumps(
            {'caption': {}, 'watermark': [{'box_2d': [900, 700, 980, 990], 'label': 'signature'}]}), encoding='utf-8')
        layout = delivery.export_training_layout(job['id'])
        with (layout / 'folders.csv').open(encoding='utf-8') as handle:
            rows = {row['path']: row for row in csv.DictReader(handle)}
        self.assertEqual(rows[slug]['mask_path'], f'masks/{slug}')
        self.assertTrue(all(row['mask_path'] == '' for path, row in rows.items() if path != slug))
        masks = sorted((layout / 'masks' / slug).glob('*.png'))
        self.assertEqual([m.stem for m in masks], sorted(Path(row[1]).stem for row in images))
        from PIL import Image
        with Image.open(layout / 'masks' / slug / f'{Path(images[0][1]).stem}.png') as mask:
            w, h = mask.size
            self.assertAlmostEqual(w / h, images[0][2] / images[0][3], places=2)
            self.assertEqual(mask.getpixel((int(w * 0.85), int(h * 0.94))), 0)      # inside the box
            self.assertEqual(mask.getpixel((w // 2, h // 2)), 255)                    # the rest learns
        with Image.open(layout / 'masks' / slug / f'{Path(images[-1][1]).stem}.png') as mask:
            self.assertEqual(mask.getextrema(), (255, 255) if len(images) > 1 else (0, 255))
        self.assertIn('masks/', (layout / 'README.txt').read_text(encoding='utf-8'))

        # Only artists without a collection: every artist of this run has one, so there is nothing to download.
        with self.assertRaises(ValueError):
            delivery.create_delivery(self.run_id, 'Planner', self.root, new_artists_only=True)

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
