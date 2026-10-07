import asyncio
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from app.models import RemotePost
from app.services import planner_harvest as harvest
from app.services import planner_store as store
from app.services import planner_tags
from app.services.planner_select import (
    PlannerConfig, RarityIndex, candidate_from_row, plan_family, rejection, score_artist,
)


def post(remote_id, artist_id=1, site='danbooru', **values):
    row = {'site': site, 'remote_id': str(remote_id), 'artist_id': artist_id, 'md5': f'{site}-{remote_id}',
           'width': 1200, 'height': 1600, 'ext': 'jpg', 'rating': 'general', 'score': 10, 'fav_count': remote_id,
           'parent_id': None, 'has_children': 0, 'created_at': '2025-01-01T00:00:00', 'artists': 'artist_a',
           'characters': '', 'copyrights': '', 'species': '', 'general': f'1girl solo tag_{remote_id % 7}',
           'meta': '', 'file_url': f'https://example/{remote_id}.jpg', 'preview_url': f'https://example/p{remote_id}.jpg'}
    row.update(values)
    return row


def plan(rows_by_artist, config=None, targets=()):
    config = config or PlannerConfig(min_images=3, max_images=5, character_floor=0)
    rarity = RarityIndex(config.rarity_min_df)
    pools, usable = {}, {}
    for artist_id, rows in rows_by_artist.items():
        candidates = [candidate_from_row(r, locked=r.get('locked', False)) for r in rows if rejection(r, config, config.tag_set('blocked', 'danbooru')) is None or r.get('locked')]
        for c in candidates:
            rarity.add(c.general)
        usable[artist_id] = len({c.family for c in candidates})
        pools[artist_id] = candidates
    pools = {a: score_artist(c, rarity, config.tag_set('boost', 'danbooru'), set(targets), config) for a, c in pools.items()}
    return plan_family(pools, usable, config, list(targets))


class SelectionTests(unittest.TestCase):
    def test_rejections_explain_unusable_posts(self):
        config = PlannerConfig()
        blocked = config.tag_set('blocked', 'danbooru')
        # Motion posts are kept by default; import extracts their frames.
        self.assertIsNone(rejection(post(1, ext='webm'), config, blocked))
        self.assertIsNone(rejection(post(1, ext='zip'), config, blocked))
        self.assertEqual(rejection(post(1, ext='webm'), PlannerConfig(include_motion=False), blocked), 'motion')
        self.assertEqual(rejection(post(1, ext='swf'), config, blocked), 'unsupported_format')
        self.assertEqual(rejection(post(1, width=600), config, blocked), 'low_resolution')
        self.assertEqual(rejection(post(1, width=800, height=4000), config, blocked), 'aspect_ratio')
        self.assertEqual(rejection(post(1, general='comic 1girl'), config, blocked), 'blocked_tag')
        self.assertIsNone(rejection(post(1, artists='artist_a artist_b'), config, blocked))
        self.assertEqual(rejection(post(1, artists='artist_a artist_b artist_c'), config, blocked), 'too_many_artists')
        self.assertEqual(rejection(post(1, artists='artist_a artist_b'), PlannerConfig(max_credited_artists=1), blocked), 'too_many_artists')
        self.assertEqual(rejection(post(1, file_url=None), config, blocked), 'no_file')
        # e621 lists warnings in the artist category; they are not collaborators.
        self.assertIsNone(rejection(post(1, artists='artist_a artist_b conditional_dnp'), config, blocked))
        # Each tag family uses its own vocabulary.
        self.assertIsNone(rejection(post(1, general='low_res'), config, blocked))
        self.assertEqual(rejection(post(1, general='low_res'), config, config.tag_set('blocked', 'e621')), 'blocked_tag')
        self.assertEqual(rejection(post(1, rating='explicit'), PlannerConfig(allowed_ratings=['general']), blocked), 'rating')

    def test_drops_small_artists_and_caps_large_ones(self):
        result = plan({1: [post(i) for i in range(1, 3)], 2: [post(i, artist_id=2) for i in range(10, 30)]})
        self.assertEqual(result.artists[1]['status'], 'dropped')
        self.assertEqual(result.artists[1]['reason'], 'too_few_usable')
        self.assertEqual(result.artists[2]['selected'], 5)
        self.assertEqual(result.artists[2]['repeats'], 10)  # 200 exposures / 5 images, capped at max_repeats
        self.assertEqual(len(result.picks), 5)

    def test_one_image_per_parent_child_family_and_md5(self):
        rows = [post(1), post(2, parent_id='1'), post(3, parent_id='1'), post(4, md5='same'), post(5, md5='same'), post(6), post(7)]
        result = plan({1: rows}, PlannerConfig(min_images=1, max_images=10, character_floor=0))
        chosen = {p['remote_id'] for p in result.picks}
        self.assertEqual(len(chosen & {'1', '2', '3'}), 1)
        self.assertEqual(len(chosen & {'4', '5'}), 1)
        self.assertEqual(len(chosen), 4)

    def test_character_share_cap_spreads_content(self):
        rows = [post(i, characters='hero') for i in range(1, 31)] + [post(i, general=f'unique_{i}') for i in range(31, 41)]
        config = PlannerConfig(min_images=5, max_images=10, character_floor=0, character_share_cap=0.3)
        result = plan({1: rows}, config)
        hero = sum(1 for p in result.picks if int(p['remote_id']) <= 30)
        self.assertEqual(hero, 3)
        self.assertEqual(len(result.picks), 10)

    def test_share_cap_relaxes_for_single_character_artists(self):
        rows = [post(i, characters='hero') for i in range(1, 21)]
        result = plan({1: rows}, PlannerConfig(min_images=5, max_images=10, character_floor=0))
        self.assertEqual(len(result.picks), 10)
        self.assertTrue(any(p['reasons'].get('share_cap_relaxed') for p in result.picks))

    def test_character_need_favors_targets_and_top_up_fills_floor(self):
        rows = [post(i, fav_count=1000 - i) for i in range(1, 21)] + [post(i, characters='rare_hero', fav_count=0) for i in range(21, 27)]
        config = PlannerConfig(min_images=3, max_images=4, character_floor=5, weight_character=3.0, topup_max_per_artist=10)
        result = plan({1: rows}, config, targets=['rare_hero'])
        self.assertEqual(result.character_counts['rare_hero'], 5)
        roles = {p['role'] for p in result.picks}
        self.assertIn('character_topup', roles)
        self.assertEqual(result.unmet_characters, [])

    def test_priority_characters_get_a_larger_character_need(self):
        rows = [post(1, characters='normal_hero', fav_count=5), post(2, characters='gacha_hero', fav_count=5)] + [post(i, fav_count=1) for i in range(3, 10)]
        config = PlannerConfig(min_images=1, max_images=1, character_floor=5, weight_character=1.0, weight_novelty=0, weight_rarity=0, weight_boost=0, character_topup=False)
        rarity = RarityIndex(config.rarity_min_df)
        pool = score_artist([candidate_from_row(r) for r in rows], rarity, set(), {'normal_hero', 'gacha_hero'}, config)
        result = plan_family({1: pool}, {1: 9}, config, ['gacha_hero', 'normal_hero'], priority={'gacha_hero'})
        self.assertEqual([p['remote_id'] for p in result.picks], ['2'])
        self.assertEqual(result.picks[0]['reasons']['character_need'], 1.5)

    def test_locks_are_kept_and_count_toward_quota(self):
        rows = [post(i) for i in range(1, 11)]
        rows[0]['locked'] = True
        rows[0]['width'] = 100  # a locked image bypasses quality filters
        result = plan({1: rows}, PlannerConfig(min_images=3, max_images=4, character_floor=0))
        self.assertIn('1', {p['remote_id'] for p in result.picks})
        self.assertEqual([p['role'] for p in result.picks].count('locked'), 1)
        self.assertEqual(len(result.picks), 4)

    def test_selection_is_deterministic(self):
        rows = {a: [post(i, artist_id=a, characters='hero' if i % 3 else '') for i in range(a * 100, a * 100 + 30)] for a in (1, 2, 3)}
        config = PlannerConfig(min_images=3, max_images=8, character_floor=10)
        first = [(p['artist_id'], p['remote_id']) for p in plan(rows, config, ['hero']).picks]
        second = [(p['artist_id'], p['remote_id']) for p in plan(rows, config, ['hero']).picks]
        self.assertEqual(first, second)


class PlannerStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = os.environ.get('ARTIST_PLANNER_PATH')
        os.environ['ARTIST_PLANNER_PATH'] = self.temp.name

    def tearDown(self):
        if self.old is None:
            os.environ.pop('ARTIST_PLANNER_PATH', None)
        else:
            os.environ['ARTIST_PLANNER_PATH'] = self.old
        self.temp.cleanup()

    def insert_posts(self, artist_id, rows):
        conn = store.connect()
        conn.executemany(f"INSERT INTO post ({','.join(store.POST_COLUMNS)}) VALUES ({','.join('?' * len(store.POST_COLUMNS))})",
                         [tuple({**r, 'artist_id': artist_id}[c] for c in store.POST_COLUMNS) for r in rows])
        conn.commit()
        conn.close()

    def test_artist_import_upserts_and_disables_missing(self):
        first = store.import_artists('site,display_name,query_tag,tag_id,post_count\ndanbooru,artist a,artist_a,1,50\ne621,b,b_(artist),2,30\n')
        self.assertEqual((first['added'], first['updated']), (2, 0))
        second = store.import_artists('﻿site,display_name,query_tag\ndanbooru,Artist A,artist a\n')
        self.assertEqual((second['added'], second['updated'], second['disabled']), (0, 1, 1))
        bad = store.import_artists('site,display_name,query_tag\npixiv,x,y\n')
        self.assertEqual(bad['imported'], 0)
        self.assertTrue(bad['errors'])
        status = store.status()
        self.assertEqual(status['artists']['danbooru']['enabled'], 1)
        self.assertEqual(status['artists']['e621']['disabled'], 1)

    def test_enable_batch_keeps_collections_and_adds_the_next_artists(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,d1,d1\ne621,e1,e1\ndanbooru,d2,d2\ndanbooru,d3,d3\n'
                             'gelbooru,g1,g1\ne621,e2,e2\n')
        conn = store.connect()
        ids = {r['tag']: r['id'] for r in conn.execute('SELECT id, tag FROM artist')}
        conn.execute('PRAGMA foreign_keys = OFF')  # a bare delivery_item stands in for an earlier delivery
        conn.executemany("INSERT INTO delivery_item (delivery_id, site, remote_id, artist_id, folder_id, status) VALUES (1, ?, '1', ?, 1, 'done')",
                         [('danbooru', ids['d1']), ('e621', ids['e1'])])  # the pilot
        conn.commit()
        conn.close()
        store.enable_only(['d1', 'e621,e1'])  # planning was limited to the pilot

        def enabled():
            conn = store.connect()
            names = {r['tag'] for r in conn.execute('SELECT tag FROM artist WHERE enabled=1')}
            conn.close()
            return names

        first = store.enable_batch(['danbooru', 'gelbooru'], add=2)
        self.assertEqual(enabled(), {'d1', 'e1', 'd2', 'd3'})  # pilot on any site + the next two
        self.assertEqual((first['added'], first['with_collections'], first['remaining']), (2, 2, 1))
        second = store.enable_batch(['danbooru', 'gelbooru'], add=2)
        self.assertEqual(enabled(), {'d1', 'e1', 'd2', 'd3', 'g1'})
        self.assertEqual(second['remaining'], 0)
        with self.assertRaises(ValueError):
            store.enable_batch(['pixiv'])

    def test_character_import_merges_danbooru_and_gelbooru_family(self):
        result = store.import_characters('site,tag,post_count\ndanbooru,hatsune miku,10\ngelbooru,hatsune_miku,5\ne621,judy_hopps,3\n')
        self.assertEqual(result['by_family'], {'danbooru': 1, 'e621': 1})

    def test_run_plan_and_export_manifest(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,a,artist_a\ne621,b,artist_b\ngelbooru,c,artist_c\n')
        store.import_characters('site,tag\ndanbooru,hero\ne621,beast\n')
        self.insert_posts(1, [post(i, characters='hero' if i < 5 else '') for i in range(1, 31)])
        self.insert_posts(2, [post(i, site='e621', rating='explicit', species='canine', characters='beast') for i in range(1, 26)])
        self.insert_posts(3, [post(i, site='gelbooru') for i in range(1, 5)])
        store.set_override(1, 'danbooru', '30', 'ban')
        run_id = store.run_plan(PlannerConfig())
        run = store.get_run(run_id)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['summary']['families']['danbooru']['artists_kept'], 1)
        self.assertEqual(run['summary']['families']['danbooru']['artists_dropped'], {'too_few_usable': 1})
        self.assertEqual(run['summary']['families']['e621']['images'], 25)
        detail = store.artist_detail(1, run_id)
        self.assertNotIn('30', {s['remote_id'] for s in detail['selected']})
        self.assertEqual(len(detail['selected']), 29)
        target = store.export_manifest(run_id)
        lines = [json.loads(line) for line in (target / 'manifest.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(lines), run['summary']['images'])
        self.assertEqual({line['site'] for line in lines}, {'danbooru', 'e621'})
        self.assertTrue((target / 'selected_e621_ids.txt').exists())
        self.assertEqual(lines[0]['credited_artists'], ['artist_a'])
        self.assertFalse(lines[0]['motion'])
        listed = store.list_artists(run_id=run_id, run_status='dropped')
        self.assertEqual([a['tag'] for a in listed['items']], ['artist_c'])

    def test_priority_characters_rank_first_and_survive_top_list_refresh(self):
        store.replace_family_characters('danbooru', [{'tag': 'miku'}, {'tag': 'reimu'}], 'top')
        result = store.add_priority_characters('danbooru', [{'tag': 'reimu'}, {'tag': 'hina_(blue_archive)', 'post_count': 99}], 'series:blue_archive')
        self.assertEqual((result['priority'], result['added']), (2, 1))
        conn = store.connect()
        order = [r[0] for r in conn.execute("SELECT tag FROM character_target WHERE family='danbooru' ORDER BY priority DESC, rank")]
        conn.close()
        self.assertEqual(order[2], 'miku')
        self.assertEqual(set(order[:2]), {'reimu', 'hina_(blue_archive)'})
        # Refreshing the most-posted list keeps priority targets.
        store.replace_family_characters('danbooru', [{'tag': 'marisa'}], 'top')
        self.assertEqual(store.status()['priority_characters'], {'danbooru': 2})
        # Clearing removes series-only targets and unmarks the rest.
        self.assertEqual(store.clear_priority_characters(), 1)
        conn = store.connect()
        self.assertEqual(sorted(r[0] for r in conn.execute('SELECT tag FROM character_target')), ['marisa', 'reimu'])
        conn.close()

    def test_recover_marks_interrupted_work(self):
        conn = store.connect()
        conn.execute("INSERT INTO run (status, config, created_at) VALUES ('running', '{}', 'x')")
        conn.execute("INSERT INTO artist (site, tag, display_name, harvest_status) VALUES ('danbooru', 'a', 'a', 'running')")
        conn.commit()
        conn.close()
        store.recover()
        conn = store.connect()
        self.assertEqual(conn.execute('SELECT status FROM run').fetchone()[0], 'interrupted')
        self.assertEqual(conn.execute('SELECT harvest_status FROM artist').fetchone()[0], 'pending')
        conn.close()


class FakeProvider:
    """Serves 2-post pages; the third page of every artist is empty."""

    def __init__(self, site, fail_tags=()):
        self.site = site
        self.fail_tags = set(fail_tags)
        self.calls = []

    async def search(self, tag, cursor, limit, sort='latest'):
        self.calls.append((tag, cursor))
        if tag in self.fail_tags:
            raise ValueError('fixture failure')
        page = int(cursor or 0)
        if page >= 2:
            return [], None
        posts = []
        for index in range(2):
            remote_id = str(page * 2 + index + 1)
            raw = {'fav_count': 3, 'rating': 'g' if self.site == 'danbooru' else 's'}
            if self.site == 'e621':
                raw['relationships'] = {'parent_id': 7, 'has_children': False}
            posts.append(RemotePost(provider=self.site, remote_id=remote_id, remote_url='u', image_url=f'https://x/{remote_id}.png',
                                    preview_url='p', width=1000, height=1000, format='png', md5=f'{tag}{remote_id}',
                                    tags={'artist': [tag], 'general': ['1girl']}, rating='safe', score=1,
                                    created_at='2025-01-01', raw_metadata=raw))
        return posts, str(page + 1)

    async def close(self):
        pass


class HarvestTests(unittest.IsolatedAsyncioTestCase):
    setUp = PlannerStoreTests.setUp
    tearDown = PlannerStoreTests.tearDown

    async def test_harvest_collects_pages_and_records_errors(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,a,artist_a\ndanbooru,broken,broken\ne621,b,artist_b\n')
        providers = {}

        def factory(site):
            providers[site] = FakeProvider(site, fail_tags={'broken'})
            return providers[site]

        job = harvest.create_job(['danbooru', 'e621'], 100, False)
        await harvest.run_job(job['id'], asyncio.Event(), factory)
        status = store.status()
        self.assertEqual(status['harvest_job']['status'], 'completed')
        self.assertEqual(status['posts'], {'danbooru': 4, 'e621': 4})
        self.assertEqual(status['artists']['danbooru']['harvest'], {'done': 1, 'error': 1})
        conn = store.connect()
        row = conn.execute("SELECT rating, parent_id, fav_count FROM post WHERE site='e621' LIMIT 1").fetchone()
        self.assertEqual(tuple(row), ('safe', '7', 3))
        self.assertEqual(conn.execute("SELECT rating FROM post WHERE site='danbooru' LIMIT 1").fetchone()[0], 'general')
        conn.close()
        # Non-network errors are not retried within a job. A second job retries
        # the failed artist once and leaves finished artists alone.
        job = harvest.create_job(['danbooru'], 100, False)
        await harvest.run_job(job['id'], asyncio.Event(), factory)
        self.assertEqual([call[0] for call in providers['danbooru'].calls], ['broken'])

    async def test_cancel_keeps_cursor_for_resume_and_max_posts_caps_harvest(self):
        store.import_artists('site,display_name,query_tag\ndanbooru,a,artist_a\n')
        stop = asyncio.Event()

        class StopAfterFirstPage(FakeProvider):
            async def search(self, tag, cursor, limit, sort='latest'):
                result = await super().search(tag, cursor, limit, sort)
                stop.set()
                return result

        job = harvest.create_job(['danbooru'], 100, False)
        await harvest.run_job(job['id'], stop, StopAfterFirstPage)
        conn = store.connect()
        artist = conn.execute('SELECT harvest_status, harvest_cursor, harvested_posts FROM artist').fetchone()
        conn.close()
        self.assertEqual(tuple(artist), ('pending', '1', 2))
        self.assertEqual(store.status()['harvest_job']['status'], 'canceled')
        job = harvest.create_job(['danbooru'], 3, False)
        await harvest.run_job(job['id'], asyncio.Event(), FakeProvider)
        conn = store.connect()
        artist = conn.execute('SELECT harvest_status, harvested_posts FROM artist').fetchone()
        conn.close()
        self.assertEqual(tuple(artist), ('done', 3))


class TagLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old_interval = planner_tags.REQUEST_INTERVAL
        planner_tags.REQUEST_INTERVAL = 0

    async def asyncTearDown(self):
        planner_tags.REQUEST_INTERVAL = self.old_interval

    async def test_check_tags_reports_aliases_deprecations_and_missing(self):
        responses = {
            'tags.json': [{'name': 'from_below', 'category': 0, 'post_count': 10, 'is_deprecated': False},
                          {'name': 'action', 'category': 0, 'post_count': 5, 'is_deprecated': True},
                          {'name': 'thumbnail', 'category': 5, 'post_count': 0, 'is_deprecated': False}],
            'tag_aliases.json': [{'antecedent_name': 'incoming_attack', 'consequent_name': 'attacking_viewer'}],
        }

        async def fake_get(self, url, params):
            return responses[url.rsplit('/', 1)[1]]

        with unittest.mock.patch.object(planner_tags._Paced, 'get', fake_get):
            items = await planner_tags.check_tags('danbooru', ['from below', 'action', 'thumbnail', 'incoming_attack', 'made_up'])
        status = {item['tag']: item['status'] for item in items}
        self.assertEqual(status, {'from_below': 'ok', 'action': 'deprecated', 'thumbnail': 'empty',
                                  'incoming_attack': 'alias', 'made_up': 'missing'})
        self.assertEqual(items[3]['replacement'], 'attacking_viewer')

    async def test_top_characters_pages_and_skips_placeholders(self):
        pages = [[{'name': 'fan_character', 'post_count': 99}, {'name': 'judy_hopps', 'post_count': 50}],
                 [{'name': 'nick_wilde', 'post_count': 40}, {'name': 'toriel', 'post_count': 30}], []]
        seen = []

        async def fake_get(self, url, params):
            seen.append(params['page'])
            return pages[params['page'] - 1]

        with unittest.mock.patch.object(planner_tags._Paced, 'get', fake_get):
            rows = await planner_tags.top_characters('e621', 3)
        self.assertEqual([row['tag'] for row in rows], ['judy_hopps', 'nick_wilde', 'toriel'])
        self.assertEqual(seen, [1, 2])


    async def test_danbooru_series_combines_related_overlap_and_qualified_names(self):
        def tag(name, count, category=4):
            return {'name': name, 'post_count': count, 'category': category, 'is_deprecated': False}

        async def fake_get(self, url, params):
            if url.endswith('related_tag.json'):
                return [{'tag': tag('hakurei_reimu', 100), 'overlap_coefficient': 1.0},
                        {'tag': tag('hatsune_miku', 900), 'overlap_coefficient': 0.01},
                        {'tag': tag('touhou_project_group', 50, 3), 'overlap_coefficient': 1.0}]
            return [tag('rare_girl_(touhou)', 4)] if params['page'] == 1 else []

        with unittest.mock.patch.object(planner_tags._Paced, 'get', fake_get):
            rows = await planner_tags.series_characters('danbooru', 'touhou')
        self.assertEqual(rows, [{'tag': 'hakurei_reimu', 'post_count': 100}, {'tag': 'rare_girl_(touhou)', 'post_count': 4}])

    async def test_e621_series_follows_sub_series_implications(self):
        implications = {'my_little_pony': ['friendship_is_magic', 'bat_pony'], 'friendship_is_magic': ['twilight_sparkle_(mlp)', 'cutie_mark']}
        categories = {'friendship_is_magic': 3, 'bat_pony': 5, 'twilight_sparkle_(mlp)': 4, 'cutie_mark': 0}

        async def fake_get(self, url, params):
            if url.endswith('tag_implications.json'):
                return [{'antecedent_name': name} for name in implications.get(params['search[consequent_name]'], [])]
            return [{'name': name, 'category': categories[name], 'post_count': 10} for name in params['search[name]'].split(',')]

        with unittest.mock.patch.object(planner_tags._Paced, 'get', fake_get):
            rows = await planner_tags.series_characters('e621', 'my_little_pony')
        self.assertEqual(rows, [{'tag': 'twilight_sparkle_(mlp)', 'post_count': 10}])


    async def test_server_errors_are_retried(self):
        import httpx
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(500, request=request)
            return httpx.Response(200, json=[{'name': 'judy_hopps', 'post_count': 5}], request=request)

        old_delay = planner_tags.RETRY_DELAY
        planner_tags.RETRY_DELAY = 0
        try:
            paced = planner_tags._Paced()
            paced.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self.assertEqual(await paced.get('https://e621.net/tags.json', {}), [{'name': 'judy_hopps', 'post_count': 5}])
            await paced.close()
        finally:
            planner_tags.RETRY_DELAY = old_delay
        self.assertEqual(len(calls), 3)

class SeriesJobTests(unittest.IsolatedAsyncioTestCase):
    setUp = PlannerStoreTests.setUp
    tearDown = PlannerStoreTests.tearDown

    async def test_series_job_resolves_aliases_skips_non_series_and_marks_priority(self):
        from app.routes import planner as routes

        async def fake_check(family, tags):
            table = {'umamusume': {'status': 'alias', 'replacement': 'equine_humanoid'},
                     'equine_humanoid': {'status': 'ok', 'category': 'species'},
                     'old_series': {'status': 'alias', 'replacement': 'my_little_pony'},
                     'my_little_pony': {'status': 'ok', 'category': 'copyright'}}
            return [{'tag': tag, **table[tag]} for tag in tags]

        async def fake_series(family, series):
            return [{'tag': 'twilight_sparkle_(mlp)', 'post_count': 500}, {'tag': 'tiny_pony_(mlp)', 'post_count': 3}]

        routes._series_job.clear()
        routes._series_job.update(status='running', total=2, done=0, current=None, results={}, error=None)
        with unittest.mock.patch.object(planner_tags, 'check_tags', fake_check),                 unittest.mock.patch.object(planner_tags, 'series_characters', fake_series):
            await routes._run_series(routes.SeriesRequest(e621=['umamusume', 'old_series'], min_posts=30))
        job = routes._series_job
        self.assertEqual(job['status'], 'completed', job.get('error'))
        result = job['results']['e621']
        self.assertEqual(result['series'], {'my_little_pony': 1})
        self.assertIn('umamusume', result['skipped'])
        self.assertEqual(store.status()['priority_characters'], {'e621': 1})

    async def test_one_failing_series_does_not_discard_the_others(self):
        import httpx
        from app.routes import planner as routes

        async def fake_check(family, tags):
            return [{'tag': tag, 'status': 'ok', 'category': 'copyright'} for tag in tags]

        async def fake_series(family, series):
            if series == 'fire_emblem':
                raise httpx.HTTPError('500 Internal Server Error')
            return [{'tag': f'hero_({series})', 'post_count': 500}]

        routes._series_job.clear()
        routes._series_job.update(status='running', total=2, done=0, current=None, results={}, error=None)
        with unittest.mock.patch.object(planner_tags, 'check_tags', fake_check),                 unittest.mock.patch.object(planner_tags, 'series_characters', fake_series):
            await routes._run_series(routes.SeriesRequest(danbooru=['fire_emblem', 'touhou']))
        result = routes._series_job['results']['danbooru']
        self.assertEqual(routes._series_job['status'], 'completed')
        self.assertEqual(result['series'], {'touhou': 1})
        self.assertIn('fire_emblem', result['skipped'])

if __name__ == '__main__':
    unittest.main()
