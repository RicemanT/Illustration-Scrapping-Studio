"""Reference pairs: character looks, the three pair kinds, prompt splitting, crops and held-out characters."""
import unittest

from app.services import reference_pairs as rp


def record(path, artist, characters, general, copyright=(), quality=('masterpiece',), boxes=None):
    trigger = f'@{artist}'
    tags = [trigger, *characters, *copyright, *general, *quality]
    return {'image_id': hash(path) % 10_000, 'path': path, 'folder': artist, 'artist': artist, 'tags': tags,
            'by': {'artist': [trigger], 'character': list(characters), 'copyright': list(copyright),
                   'general': list(general), 'quality': list(quality)},
            'characters': [c.casefold() for c in characters], 'boxes': boxes or {}}


LOOK = ['blonde hair', 'blue eyes', 'red dress']


class ReferencePairTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            record('a/1.png', 'ann', ['alice'], ['1girl', 'solo', *LOOK, 'beach'], ['wonderland']),
            record('a/2.png', 'ann', ['alice'], ['1girl', 'solo', *LOOK, 'forest']),
            record('b/1.png', 'bob', ['alice'], ['1girl', 'solo', 'blonde hair', 'blue eyes', 'red dress', 'night']),
            record('b/2.png', 'bob', ['alice', 'carol'], ['2girls', *LOOK, 'cafe'], boxes={'alice': [0, 0, 500, 500]}),
            record('b/3.png', 'bob', ['dave'], ['1boy', 'solo', 'black hair', 'office']),
            record('a/3.png', 'ann', [], ['scenery', 'mountain']),
        ]

    def test_looks_skip_count_and_scene_tags(self):
        looks = rp.character_looks(self.records)
        self.assertEqual(sorted(looks['alice']), sorted(LOOK))
        self.assertNotIn('1girl', looks['alice'])
        self.assertEqual(looks['dave'], [], 'one image is not enough to call a tag usual')

    def test_pair_kinds_and_prompt_split(self):
        result = rp.build(self.records)
        pairs = result['pairs']
        kinds = {(p['kind'], p['target'], p['reference']['path']) for p in pairs}
        self.assertIn(('identity_style', 'a/1.png', 'a/2.png'), kinds)
        self.assertIn(('identity', 'a/1.png', 'b/1.png'), kinds)
        self.assertNotIn(('identity_style', 'b/2.png', 'b/1.png'), kinds, 'two-character images are never targets')
        crop = next(p for p in pairs if p['kind'] == 'identity' and p['reference']['path'] == 'b/2.png')
        self.assertEqual(crop['reference']['crop'], [0, 0, 500, 500])

        same = next(p for p in pairs if p['kind'] == 'identity_style' and p['target'] == 'a/1.png')
        self.assertNotIn('@ann', same['keep'])
        self.assertNotIn('alice', same['keep'])
        self.assertNotIn('wonderland', same['keep'])
        self.assertEqual(sorted(same['look']), sorted(LOOK))
        self.assertEqual(same['keep'], ['1girl', 'solo', 'beach', 'masterpiece'])
        self.assertEqual(same['minimal'], ['1girl', 'solo', 'masterpiece'])
        other = next(p for p in pairs if p['kind'] == 'identity' and p['target'] == 'a/1.png')
        self.assertEqual(other['keep'][0], '@ann', 'another artist: the target keeps its trigger')
        self.assertIn('@ann', other['minimal'])

        style = [p for p in pairs if p['kind'] == 'style']
        self.assertTrue(style)
        for p in style:
            self.assertNotIn(p['keep'][0] if p['keep'] else '', ('@ann', '@bob'))
            target = next(r for r in self.records if r['path'] == p['target'])
            reference = next(r for r in self.records if r['path'] == p['reference']['path'])
            self.assertEqual(target['artist'], reference['artist'])
            self.assertFalse(set(target['characters']) & set(reference['characters']))

    def test_held_out_characters_are_never_paired(self):
        records = [record(f'{a}/{i}.png', a, ['zed'], ['1boy', 'solo', 'green hair']) for a in ('x', 'y') for i in range(3)]
        original = rp.HOLDOUT_SHARE
        rp.HOLDOUT_SHARE = 1.0
        try:
            result = rp.build(records)
        finally:
            rp.HOLDOUT_SHARE = original
        self.assertEqual(result['held_out'], ['zed'])
        self.assertFalse([p for p in result['pairs'] if p['character'] == 'zed'])
        self.assertEqual(len(result['eval'][0]['images']), 6)


from tests.test_folder_guard import PlannerFolderCase  # noqa: E402


class ReferencePairExportTests(PlannerFolderCase):
    async def test_export_reads_the_library(self):
        import json
        import app.db as db
        main = db.get_connection()   # a small reserve, so a nearly full test disk still admits the few test images
        main.execute("INSERT OR REPLACE INTO app_setting (key, value, updated_at) VALUES ('storage_reserve_gib', '0.5', '2026-01-01')")
        main.commit()
        main.close()
        await self.deliver()
        folder_id, ids = self.folder_images()
        from app.services.tags import TagService
        TagService(self.root).bulk_edit(folder_id, ids, ['solo'], 'add')
        records = rp.collect(rp._folders(None, [folder_id]))
        self.assertEqual(len(records), len(ids))
        self.assertTrue(all(r['by'].get('artist') for r in records), 'the trigger is found as the artist tag')
        summary = rp.export(folder_ids=[folder_id], name='test-pairs')
        self.assertEqual(summary['images'], len(ids))
        lines = (rp_path := __import__('pathlib').Path(summary['path']) / 'pairs.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines), summary['pairs'])
        if lines:
            self.assertIn(json.loads(lines[0])['kind'], ('identity_style', 'identity', 'style'))
        self.assertTrue((rp_path.parent / 'README.txt').is_file())
