import asyncio
import json
import threading
import unittest
from unittest import mock

import numpy as np

import app.db as db
from app.analysis import assess as A
from app.analysis import queue as analysis_queue
from app.analysis import review
from app.analysis import store as analysis_store
from app.services import planner_curation as curation
from app.services import planner_delivery as delivery
from app.services import planner_quality as quality
from app.services import planner_store as store
from app.services.planner_select import PlannerConfig
import test_planner
import test_planner_delivery

RNG = np.random.default_rng(3)
STYLE = {name: v / np.linalg.norm(v) for name, v in (('A', RNG.normal(size=64)), ('B', RNG.normal(size=64)), ('C', RNG.normal(size=64)))}


def styled(name: str, noise: float = 0.08) -> np.ndarray:
    # Same-style pictures are similar (cosine about 0.7) but never near-identical.
    vector = STYLE[name] + RNG.normal(scale=noise, size=64)
    return vector / np.linalg.norm(vector)


def posts_for(styles: list[str]) -> list[dict]:
    """Posts newest first: index 0 is the newest."""
    return [{'site': 'danbooru', 'remote_id': str(i + 1), 'created_at': f'{2025 - i // 12}-{12 - i % 12:02d}-01T00:00:00Z'} for i in range(len(styles))]


def analysis_rows(posts, values=None, **classes):
    rows = {}
    for index, post in enumerate(posts):
        row = {name: (values[index] if values is not None else 5.0) for name in analysis_store.SCORERS}
        for column, flagged in classes.items():
            row[column] = 0.9 if index in flagged else 0.05
        rows[(post['site'], post['remote_id'])] = row
    return rows


class AssessTests(unittest.TestCase):
    def scale(self):
        return A.AestheticScale({name: list(np.linspace(0, 10, 101)) for name in analysis_store.SCORERS})

    def test_latest_style_wins_and_old_style_is_off_style(self):
        styles = ['A'] * 60 + ['B'] * 40
        posts = posts_for(styles)
        vectors = {'dinov2': {(p['site'], p['remote_id']): styled(s) for p, s in zip(posts, styles)}}
        result = A.assess_artist(posts, analysis_rows(posts), vectors, self.scale(), A.AssessOptions(aesthetic_floor=0), target=60)
        self.assertEqual(result.era, 'latest')
        rejected = {key[1] for key, v in result.verdicts.items() if v.reject == 'off_style'}
        old_style = {p['remote_id'] for p, s in zip(posts, styles) if s == 'B'}
        self.assertTrue(old_style <= rejected)
        self.assertLessEqual(len(rejected - old_style), 3)  # a robust cut may clip the edge of the style
        kept = [v for v in result.verdicts.values() if v.reject is None]
        self.assertTrue(all(v.style > 0 for v in kept))

    def test_career_style_is_used_when_the_latest_era_is_too_small(self):
        styles = ['C'] * 20 + ['A'] * 80
        posts = posts_for(styles)
        vectors = {'dinov2': {(p['site'], p['remote_id']): styled(s) for p, s in zip(posts, styles)},
                   'dinov3': {(p['site'], p['remote_id']): styled(s) for p, s in zip(posts, styles)}}
        result = A.assess_artist(posts, analysis_rows(posts), vectors, self.scale(), A.AssessOptions(aesthetic_floor=0), target=60)
        self.assertEqual((result.era, result.latest_usable, result.career_usable), ('career', 20, 80))
        self.assertEqual({key[1] for key, v in result.verdicts.items() if v.reject == 'off_style'}, {str(i) for i in range(1, 21)})

    def test_content_categories_follow_the_majority_of_the_style(self):
        styles = ['A'] * 50
        posts = posts_for(styles)
        vectors = {'dinov2': {(p['site'], p['remote_id']): styled(s) for p, s in zip(posts, styles)}}
        # 5 rough sketches (minority) are excluded; comics are 70% of the work and stay.
        rows = analysis_rows(posts, rough=set(range(5)), cls_comic=set(range(10, 45)))
        result = A.assess_artist(posts, rows, vectors, self.scale(), A.AssessOptions(aesthetic_floor=0), target=30)
        self.assertIn('rough', result.excluded)
        self.assertNotIn('comic', result.excluded)
        verdicts = {int(k[1]): v for k, v in result.verdicts.items()}
        # Every sketch is gone (as content, or already as off-style); only sketches are dropped for content.
        self.assertTrue(all(verdicts[i].reject in ('content_rough', 'off_style') for i in range(1, 6)))
        self.assertEqual({i for i, v in verdicts.items() if v.reject == 'content_rough'} - set(range(1, 6)), set())
        self.assertFalse(any(v.reject == 'content_comic' for v in verdicts.values()))
        self.assertGreater(sum(v.reject is None for i, v in verdicts.items() if 11 <= i <= 45), 30)

    def test_aesthetic_floor_duplicates_and_flags(self):
        styles = ['A'] * 30
        posts = posts_for(styles)
        vectors = {(p['site'], p['remote_id']): styled('A') for p in posts}
        vectors[('danbooru', '2')] = vectors[('danbooru', '1')].copy()  # the same picture twice
        values = [6.0] * 30
        values[29] = 0.5  # bottom of the dataset
        values[1] = 5.0
        rows = analysis_rows(posts, values=values, ai={3})
        result = A.assess_artist(posts, rows, {'dinov2': vectors}, self.scale(), A.AssessOptions(aesthetic_floor=0.2), target=20)
        verdicts = {k[1]: v for k, v in result.verdicts.items()}
        self.assertEqual(verdicts['30'].reject, 'low_aesthetic')
        self.assertEqual(verdicts['2'].reject, 'near_duplicate')  # the lower-scored copy goes
        self.assertIsNone(verdicts['1'].reject)
        self.assertIsNone(verdicts['4'].reject)  # AI is not a content rule by default
        gated = A.assess_artist(posts, rows, {'dinov2': vectors}, self.scale(),
                                A.AssessOptions(aesthetic_floor=0.2, gates=('rough', 'ai')), target=20)
        self.assertEqual(gated.verdicts[('danbooru', '4')].reject, 'content_ai')
        self.assertIsNone(A.assess_artist(posts, {}, {}, self.scale(), A.AssessOptions(), target=20))

    def test_safety_net_keeps_artists_whose_pictures_look_alike(self):
        # Character sprites on blank backgrounds: every deep-feature vector is nearly the same, so near-duplicate
        # removal alone would leave one post. The artist must keep at least `needed` posts, flagged for review.
        styles = ['A'] * 60
        posts = posts_for(styles)
        style = {(p['site'], p['remote_id']): styled('A') for p in posts}
        same = STYLE['A'] / np.linalg.norm(STYLE['A'])
        deep = {key: same + RNG.normal(scale=0.001, size=64) for key in style}
        rows = analysis_rows(posts)
        result = A.assess_artist(posts, rows, {'dinov2': style}, self.scale(), A.AssessOptions(), target=60, duplicate_vectors=deep, needed=30)
        kept = [v for v in result.verdicts.values() if v.reject is None]
        self.assertEqual(result.relaxed, ['duplicates'])
        self.assertGreaterEqual(len(kept), 30)
        self.assertTrue(all('near_duplicate' not in v.flags for v in kept))  # nothing left to call a duplicate

    def test_safety_net_relaxes_style_only_when_still_short(self):
        # 60 posts in style A and 10 far ones: style drops the 10 in the latest and the career style alike.
        styles = ['A'] * 60 + ['B'] * 10
        posts = posts_for(styles)
        vectors = {(p['site'], p['remote_id']): styled(s) for p, s in zip(posts, styles)}
        strict = A.assess_artist(posts, analysis_rows(posts), {'dinov2': vectors}, self.scale(), A.AssessOptions(), target=60, needed=30)
        self.assertEqual(strict.relaxed, [])  # enough posts: rules stay strict
        self.assertTrue(all(v.reject == 'off_style' for k, v in strict.verdicts.items() if int(k[1]) > 60))
        # Needing 70, the style rule becomes a flag: the far posts are kept but flagged.
        short = A.assess_artist(posts, analysis_rows(posts), {'dinov2': vectors}, self.scale(), A.AssessOptions(), target=60, needed=70)
        self.assertEqual(short.relaxed, ['style'])
        kept_far = [v for k, v in short.verdicts.items() if int(k[1]) > 60 and v.reject is None]
        self.assertEqual(len(kept_far), 10)
        self.assertTrue(all('off_style' in v.flags for v in kept_far))

    def test_auc(self):
        self.assertEqual(A.auc([3, 4], [1, 2]), 1.0)
        self.assertEqual(A.auc([1], [1]), 0.5)
        self.assertIsNone(A.auc([], [1]))


class AnalysisLibraryTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_planner_delivery.DeliveryTests.setUp
    tearDown = test_planner_delivery.DeliveryTests.tearDown
    factory = test_planner_delivery.DeliveryTests.factory

    async def delivered(self):
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        main = db.get_connection()
        folder = main.execute("SELECT id FROM collection WHERE name='Artist X'").fetchone()[0]
        images = {r['remote_id']: r['id'] for r in main.execute(
            'SELECT i.id, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?', (folder,))}
        main.close()
        return folder, images

    def add_posts(self, count: int, style_of) -> None:
        """Artist X gets posts 101..100+count (newest first) with analysis rows; style_of(index) names each style."""
        conn = store.connect()
        rows = [test_planner_delivery.planner_post(100 + i, 'danbooru', 'artist_x', created_at=f'{2025 - i // 10}-06-01T00:00:00Z',
                                                   md5=f'{100 + i:032x}') for i in range(1, count + 1)]
        conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                         [tuple({**r, 'artist_id': 1}[c] for c in store.POST_COLUMNS) for r in rows])
        conn.commit()
        conn.close()
        results = []
        for i in range(1, count + 1):
            results.append({'site': 'danbooru', 'remote_id': str(100 + i), 'artist_id': 1, 'status': 'done',
                            'scores': {name: 5.0 + (i % 7) / 10 for name in analysis_store.SCORERS},
                            'vectors': {'dinov2:4-6': styled(style_of(i)), 'dinov3:4-6': styled(style_of(i))}})
        conn = analysis_store.connect()
        analysis_store.save_results(conn, results)
        conn.commit()
        conn.close()

    def analyse_delivered(self, images, off_style=()):
        results = [{'site': 'danbooru', 'remote_id': rid, 'artist_id': 1, 'status': 'done',
                    'scores': {name: 9.0 for name in analysis_store.SCORERS},
                    'vectors': {'dinov2:4-6': styled('B' if rid in off_style else 'A'), 'dinov3:4-6': styled('B' if rid in off_style else 'A')}}
                   for rid in images]
        conn = analysis_store.connect()
        analysis_store.save_results(conn, results)
        conn.commit()
        conn.close()

    async def test_queue_picks_newest_older_and_delivered_posts_with_sample_urls(self):
        folder, images = await self.delivered()
        self.add_posts(30, lambda i: 'A')
        conn = analysis_store.connect()
        conn.execute('DELETE FROM analysis_post')
        conn.commit()
        conn.close()
        config = analysis_store.AnalysisConfig(newest_posts=10, older_posts=5)
        counts = analysis_queue.build_queue(1, [1], config)
        conn = analysis_store.connect()
        queued = {r['remote_id']: json.loads(r['urls']) for r in conn.execute('SELECT remote_id, urls FROM analysis_queue WHERE job_id=1')}
        conn.close()
        self.assertTrue(set(images) <= set(queued))  # every delivered post
        planner = store.connect()
        rows = planner.execute('SELECT * FROM post WHERE artist_id=1').fetchall()
        planner.close()
        chosen = {r['remote_id'] for r in analysis_queue.choose_posts(rows, 10, 5)}
        self.assertEqual(len(chosen), 15)
        self.assertEqual(set(queued), chosen | set(images))
        self.assertTrue({str(i) for i in range(101, 110)} <= set(queued))  # the newest posts
        self.assertEqual(counts['queued'], len(queued))
        md5 = f'{101:032x}'
        self.assertEqual(queued['101'][0], f'https://cdn.donmai.us/sample/{md5[:2]}/{md5[2:4]}/sample-{md5}.jpg')
        self.assertEqual(queued['101'][-1], 'https://example.test/101.jpg')

    async def test_worker_downloads_scores_and_stores_results(self):
        from PIL import Image
        from app.analysis import worker
        await self.delivered()
        conn = analysis_store.connect()
        job_id = conn.execute("INSERT INTO analysis_job (status, params, created_at) VALUES ('queued', ?, 'x')",
                              (json.dumps({'config': {'batch_size': 2, 'duty': 1.0, 'download_interval': 0.05}}),)).lastrowid
        conn.executemany('INSERT INTO analysis_queue (job_id, site, remote_id, artist_id, urls) VALUES (?, ?, ?, 1, ?)',
                         [(job_id, 'danbooru' if i <= 5 else 'e621', str(i), json.dumps([f'https://example.test/{i}.jpg'])) for i in range(1, 9)])
        conn.commit()
        conn.close()
        threads = {}

        def fake_fetch(client, pacer, site, urls, keep=None):
            threads.setdefault(site, set()).add(threading.current_thread().name)
            if urls[0].endswith('/3.jpg'):
                return None, 'HTTP 404'
            return Image.new('RGB', (64, 64), (10, 20, 30)), urls[0]

        # GPU 0 starts hot: it takes no batches until it has cooled, while GPU 1 keeps working.
        checks = []

        def temperatures():
            checks.append(1)
            return {0: 86.0 if len(checks) <= 4 else 60.0, 1: 50.0}

        def fake_load(config, devices, report):
            report('fake', f'ok on {devices[0]}')
            return [('fake', devices[0])]

        used = set()

        def fake_run(models, images):
            used.add(models[0][1])
            return [{'scores': {'ws3': 7.5, 'rough': 0.1}, 'vectors': {'dinov2:4-6': np.ones(8)}, 'models': ['fake'], 'era': '2020s'} for _ in images]

        with mock.patch.object(worker, 'fetch_image', fake_fetch), mock.patch('app.analysis.models.load_models', fake_load), \
                mock.patch('app.analysis.models.run_models', fake_run), mock.patch.object(worker, 'gpu_problem', lambda config: None), \
                mock.patch.object(worker, 'gpu_temperatures', temperatures), mock.patch.object(worker, 'COOLING_POLL', 0.05), \
                mock.patch.object(worker, 'devices', lambda config: ['cuda:0', 'cuda:1']):
            self.assertEqual(worker.run_job(job_id), 0)
        conn = analysis_store.connect()
        job = conn.execute('SELECT * FROM analysis_job WHERE id=?', (job_id,)).fetchone()
        posts = {r['remote_id']: dict(r) for r in conn.execute('SELECT * FROM analysis_post')}
        vectors = conn.execute('SELECT count(*) FROM analysis_vector').fetchone()[0]
        conn.close()
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(json.loads(job['progress'])['done'], 7)
        self.assertEqual(json.loads(job['progress'])['phase'], 'completed')
        self.assertEqual((posts['3']['status'], posts['3']['error']), ('failed', 'HTTP 404'))
        self.assertEqual((posts['1']['ws3'], posts['1']['era']), (7.5, '2020s'))
        self.assertEqual(posts['7']['status'], 'done')
        self.assertEqual(vectors, 7)
        self.assertGreaterEqual(len(checks), 2)
        self.assertIn('cuda:1', used)
        progress = json.loads(job['progress'])
        self.assertEqual((progress['gpus'], progress['models']['fake']), (2, 'ok on cuda:0, cuda:1'))
        # Each site downloads on its own threads.
        self.assertTrue(all(name.startswith('download-danbooru') for name in threads['danbooru']))
        self.assertTrue(all(name.startswith('download-e621') for name in threads['e621']))
        self.assertEqual(analysis_store.stats()['score_summary']['ws3'], {'count': 7, 'mean': 7.5, 'min': 7.5, 'max': 7.5})

    async def test_new_models_reuse_kept_samples_and_keep_earlier_scores(self):
        import io as _io
        from PIL import Image
        from app.analysis import worker
        buffer = _io.BytesIO()
        Image.new('RGB', (32, 32), (200, 10, 10)).save(buffer, format='PNG')

        def job(models_output):
            conn = analysis_store.connect()
            job_id = conn.execute("INSERT INTO analysis_job (status, params, created_at) VALUES ('queued', ?, 'x')",
                                  (json.dumps({'config': {'batch_size': 2, 'duty': 1.0, 'gpus': ''}}),)).lastrowid
            conn.executemany('INSERT INTO analysis_queue (job_id, site, remote_id, artist_id, urls) VALUES (?, ?, ?, 1, ?)',
                             [(job_id, 'danbooru', str(i), json.dumps([f'https://example.test/{i}.png'])) for i in (101, 102)])
            conn.commit()
            conn.close()
            return job_id

        downloads = []

        def first_fetch(client, pacer, site, urls, keep=None):
            downloads.append(urls[0])
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_bytes(buffer.getvalue())
            return worker.open_sample(buffer.getvalue()), urls[0]

        def no_fetch(*args, **kwargs):
            raise AssertionError('kept samples must not be downloaded again')

        def runner(scores, name):
            return lambda models, images: [{'scores': dict(scores), 'vectors': {}, 'models': [name]} for _ in images]

        load = lambda config, devices, report: [('fake', object())]
        with mock.patch('app.analysis.models.load_models', load), mock.patch.object(worker, 'fetch_image', first_fetch), \
                mock.patch('app.analysis.models.run_models', runner({'ws3': 7.5}, 'ws3')):
            self.assertEqual(worker.run_job(job({'ws3': 7.5})), 0)
        self.assertEqual(len(downloads), 2)
        self.assertEqual(analysis_store.stats()['samples']['count'], 2)
        # A model ticked later: the samples come from disk and the earlier scores stay.
        with mock.patch('app.analysis.models.load_models', load), mock.patch.object(worker, 'fetch_image', no_fetch), \
                mock.patch('app.analysis.models.run_models', runner({'anzhc': 9.0, 'anzhc_class': 10}, 'anzhc')):
            self.assertEqual(worker.run_job(job({'anzhc': 9.0})), 0)
        conn = analysis_store.connect()
        row = dict(conn.execute("SELECT * FROM analysis_post WHERE remote_id='101'").fetchone())
        analysis_store.save_results(conn, [{'site': 'danbooru', 'remote_id': '101', 'status': 'failed', 'error': 'HTTP 404'}])
        after = conn.execute("SELECT status FROM analysis_post WHERE remote_id='101'").fetchone()[0]
        conn.close()
        self.assertEqual((row['ws3'], row['anzhc'], row['anzhc_class'], row['models']), (7.5, 9.0, 10.0, 'ws3,anzhc'))
        self.assertEqual(row['source'], 'https://example.test/101.png')
        self.assertEqual(after, 'done')  # a failed re-download never replaces results

    def test_broken_vectors_are_purged_once_and_never_used(self):
        conn = analysis_store.connect()
        good, bad = np.ones(8), np.full(8, np.nan)
        analysis_store.save_results(conn, [{'site': 'danbooru', 'remote_id': str(i), 'status': 'done', 'models': ['dinov2', 'dinov3'],
                                            'vectors': {'dinov2:4-6': good, 'dinov3:4-6': bad}} for i in (1, 2)])
        conn.commit()
        keys = [('danbooru', '1'), ('danbooru', '2')]
        self.assertEqual(analysis_store.load_vectors(conn, keys, 'dinov3:4-6'), {})
        self.assertEqual(len(analysis_store.load_vectors(conn, keys, 'dinov2:4-6')), 2)
        self.assertEqual(analysis_store.purge_invalid_vectors(conn), 2)
        self.assertEqual(analysis_store.purge_invalid_vectors(conn), 0)  # once per database
        models = conn.execute("SELECT models FROM analysis_post WHERE remote_id='1'").fetchone()[0]
        count = conn.execute("SELECT count(*) FROM analysis_vector WHERE model LIKE 'dinov3:%'").fetchone()[0]
        conn.close()
        self.assertEqual((models, count), ('dinov2', 0))  # the next run redoes just DINOv3

    def test_models_on_different_gpus_run_at_the_same_time(self):
        from app.analysis.models import run_models
        barrier = threading.Barrier(2)

        class Fake:
            def __init__(self, device, key):
                self.device, self.key = device, key

            def __call__(self, images):
                barrier.wait(timeout=5)  # both devices must be inside a model at once, or this times out
                return [{self.key: 1.0, 'vec': np.ones(2)} if self.key == 'ws3' else {self.key: 2.0} for _ in images]

        results = run_models([('style', Fake('cuda:0', 'polished')), ('scorer', Fake('cuda:1', 'ws3'))], ['a', 'b'])
        self.assertEqual(results[0]['scores'], {'polished': 2.0, 'ws3': 1.0})
        self.assertEqual(results[1]['models'], ['style', 'scorer'])
        self.assertEqual(list(results[0]['vectors']), ['vec'])

    async def test_worker_refuses_to_run_on_the_cpu_when_gpus_are_set(self):
        from app.analysis import worker
        conn = analysis_store.connect()
        job_id = conn.execute("INSERT INTO analysis_job (status, params, created_at) VALUES ('queued', '{}', 'x')").lastrowid
        conn.commit()
        conn.close()
        loaded = mock.Mock()
        report = {'wanted': [0, 1], 'available': False, 'torch': '2.14.1', 'torch_cuda': '13.0', 'reason': 'driver too old'}
        with mock.patch.object(worker, 'cuda_report', lambda config: report), mock.patch('app.analysis.models.load_models', loaded):
            self.assertEqual(worker.run_job(job_id), 1)
        conn = analysis_store.connect()
        job = conn.execute('SELECT * FROM analysis_job WHERE id=?', (job_id,)).fetchone()
        conn.close()
        self.assertEqual(job['status'], 'failed')
        self.assertIn('driver too old', job['error'])
        loaded.assert_not_called()

    def test_torch_index_matches_the_driver(self):
        from app.analysis.service import torch_index
        self.assertTrue(torch_index('13.0').endswith('/cu130'))
        self.assertTrue(torch_index('12.2').endswith('/cu126'))
        self.assertTrue(torch_index('11.8').endswith('/cu118'))
        self.assertIsNone(torch_index('11.4'))
        self.assertIsNone(torch_index(None))
        from app.analysis.service import onnxruntime_constraint
        self.assertEqual(onnxruntime_constraint('12.8'), 'onnxruntime-gpu>=1.20,<1.27')  # 1.27+ needs CUDA 13
        self.assertIsNone(onnxruntime_constraint('13.0'))
        self.assertIsNone(onnxruntime_constraint(None))

    async def test_plan_with_analysis_drops_off_style_posts(self):
        await self.delivered()
        # Newest 40 posts in style A, 10 older ones in style B.
        self.add_posts(50, lambda i: 'A' if i <= 40 else 'B')
        run_id = store.run_plan(PlannerConfig(min_images=3, max_images=20, character_floor=0, use_analysis=True, aesthetic_floor=0))
        conn = store.connect()
        picked = {int(r['remote_id']) for r in conn.execute("SELECT remote_id FROM selection WHERE run_id=? AND site='danbooru'", (run_id,))}
        summary = json.loads(conn.execute('SELECT summary FROM run WHERE id=?', (run_id,)).fetchone()[0])
        reasons = json.loads(conn.execute("SELECT reasons FROM selection WHERE run_id=? AND site='danbooru' LIMIT 1", (run_id,)).fetchone()[0])
        conn.close()
        self.assertEqual(len(picked), 20)
        self.assertTrue(all(101 <= p <= 140 for p in picked))  # no style-B or unanalysed posts
        self.assertGreater(summary['families']['danbooru']['rejected_posts']['off_style'], 0)
        self.assertEqual(summary['families']['danbooru']['analysis']['eras'], {'latest': 1})
        self.assertIn('style', reasons)
        # The review panel says why each post was skipped.
        detail = store.artist_detail(1, run_id)
        self.assertGreater(detail['skipped'].get('off_style', 0), 0)
        self.assertGreaterEqual(detail['skipped'].get('usable', 0), 30)
        skips = {item['remote_id']: item.get('skip') for item in detail['runners_up']}
        self.assertTrue(any(reason == 'off_style' for reason in skips.values()))

    async def test_review_flags_calibration_reset_and_aesthetic_tags(self):
        folder, images = await self.delivered()
        self.add_posts(30, lambda i: 'A')
        removed_post = '2'
        self.analyse_delivered(images, off_style={removed_post, '4'})
        detail = review.folder_review(folder)
        flagged = {images[rid] for rid in ('2', '4')}
        self.assertTrue(all('off_style' in detail['images'][image_id]['flags'] for image_id in flagged))
        main = db.get_connection()
        counts = dict(main.execute('SELECT id, analysis_flag_count FROM image WHERE folder_id=?', (folder,)).fetchall())
        main.close()
        self.assertTrue(all(counts[image_id] >= 1 for image_id in flagged))
        from app.services.filters import LocalFilter, LocalQuery, list_images
        ordered = [i['id'] for i in list_images(folder, LocalQuery(sort='flags'))['items']]
        self.assertEqual(set(ordered[:2]), flagged)
        self.assertEqual({i['id'] for i in list_images(folder, LocalQuery(filters=LocalFilter(flagged=True)))['items']}, flagged)

        # The hand curation removed post 2 (off-style); calibration sees the style model agreeing.
        from app.services.collections import CollectionService
        CollectionService().remove_images(folder, [images[removed_post], images['4']], self.root, 'ab' * 16)
        store.set_override(1, 'danbooru', '5', 'lock')
        report = review.calibration('live')
        best = next(r for r in report['style'] if r['model'] == 'dinov2+dinov3:4-6')
        self.assertEqual((best['removed'], best['auc']), (2, 1.0))

        result = review.reset_curation([1])
        from pathlib import Path
        backup = json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(backup['labels']['1']['removed'], [f'danbooru:{removed_post}', 'danbooru:4'])
        self.assertEqual((result['overrides_cleared'], result['removals_released']), (1, 2))
        self.assertEqual(delivery.ban_removed_images(), 0)  # the released removal is not banned again
        self.assertIn(removed_post, {i['remote_id'] for i in curation.candidates(folder)['items']})
        self.assertEqual(review.calibration('auto')['source'], 'backup')

        # Aesthetic marks from the scorer ensemble: the delivered posts score 9.0, the dataset's best.
        config = quality.QualityConfig(very_aesthetic_top=5, aesthetic_top=15, min_bucket=1)
        quality.build_stats(config.metric)
        outcome = quality.apply([folder], config)
        self.assertGreater(sum(outcome['aesthetic'].values()), 0)
        main = db.get_connection()
        marks = {r['aesthetic_mark'] for r in main.execute('SELECT aesthetic_mark FROM image WHERE folder_id=?', (folder,))}
        main.close()
        self.assertTrue(marks & {'very aesthetic', 'aesthetic'})


if __name__ == '__main__':
    unittest.main()
