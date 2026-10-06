import asyncio
import csv
import io
import unittest

import app.db as db
from app.services import planner_curation as curation
from app.services import planner_delivery as delivery
from app.services import planner_store as store
from app.services import tracker
import test_planner_delivery

# Artist X's posts: 1 is not selected by the plan, 3 was deleted upstream; 2, 4, 5 and 6 are delivered.
CHARACTERS = {'1': 'alice', '2': 'alice bob', '3': 'carol', '4': 'bob', '5': 'alice', '6': 'dave'}


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_planner_delivery.DeliveryTests.setUp
    tearDown = test_planner_delivery.DeliveryTests.tearDown
    factory = test_planner_delivery.DeliveryTests.factory

    async def asyncSetUp(self):
        conn = store.connect()
        for remote_id, characters in CHARACTERS.items():
            conn.execute("UPDATE post SET characters=?, copyrights='wonderland' WHERE site='danbooru' AND remote_id=?", (characters, remote_id))
        conn.executemany('INSERT INTO character_target (family, tag, post_count, rank, source, priority) VALUES (?, ?, 0, ?, ?, ?)',
                         [('danbooru', 'alice', 1, 'test', 0), ('danbooru', 'bob', 2, 'test', 1),
                          ('danbooru', 'carol', 3, 'test', 0), ('danbooru', 'erin', 4, 'test', 0)])
        conn.commit()
        conn.close()
        job = delivery.create_delivery(self.run_id, 'Planner', self.root)
        await delivery.run_delivery(job['id'], asyncio.Event(), self.factory, self.root)
        main = db.get_connection()
        self.folder = main.execute("SELECT id FROM collection WHERE name='Artist X'").fetchone()[0]
        self.images = {r['remote_id']: r['id'] for r in main.execute(
            'SELECT i.id, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?', (self.folder,))}
        main.close()

    def rows(self, kind='character', **kwargs):
        return {row['tag']: row for row in tracker.table(kind, family='danbooru', limit=500, **kwargs)['items']}

    async def test_coverage_tables_goals_and_folder_detail(self):
        tracker.refresh_all()
        rows = self.rows()
        pick = lambda tag, *keys: tuple(rows[tag][key] for key in keys)
        self.assertEqual(pick('alice', 'planned', 'now', 'spare', 'series', 'status'), (2, 2, 1, 'wonderland', 'met'))
        self.assertEqual(pick('bob', 'planned', 'now', 'priority'), (2, 2, True))
        self.assertEqual(pick('carol', 'planned', 'now', 'spare', 'status'), (1, 0, 1, 'missing'))
        self.assertEqual(pick('erin', 'now', 'status', 'target'), (0, 'missing', True))
        self.assertEqual(pick('dave', 'now', 'target'), (1, False))

        tracker.set_goal('character', 'danbooru', ['alice'], 5)
        rows = self.rows()
        self.assertEqual(pick('alice', 'goal', 'gap', 'status'), (5, 3, 'below'))
        self.assertEqual(set(self.rows(status='missing')), {'carol', 'erin'})
        self.assertEqual(list(self.rows(priority_only=True)), ['bob'])

        general = self.rows('general')
        self.assertEqual(general['1girl']['now'], 4)
        self.assertEqual(general['1girl']['spare'], 2)

        detail = tracker.folder_detail(self.folder)
        self.assertEqual({c['tag']: c['here'] for c in detail['characters']}, {'alice': 2, 'bob': 2, 'dave': 1})
        self.assertEqual([(c['tag'], c['planned_here'], c['here']) for c in detail['lost']], [('carol', 1, 0)])
        self.assertEqual(detail['characters'][0]['tag'], 'bob')  # priority first

    async def test_deletions_are_reported_and_accepting_counts_as_accepted(self):
        tracker.refresh_all()
        tracker.set_goal('character', 'danbooru', ['dave'], 2)
        from app.services.collections import CollectionService
        CollectionService().remove_images(self.folder, [self.images['6']], self.root, 'ef' * 16)
        impact = tracker.deletion_impact(self.folder, [self.images['6']])
        self.assertEqual([(c['tag'], c['now'], c['goal']) for c in impact['characters']], [('dave', 0, 2)])
        self.assertEqual(self.rows()['dave']['now'], 0)  # live, without a full refresh

        self.assertEqual(self.rows()['alice']['accepted'], 0)
        curation.set_complete(self.folder, True)
        tracker.refresh_folder(self.folder)
        rows = self.rows()
        self.assertEqual((rows['alice']['accepted'], rows['alice']['spare']), (2, 0))  # accepted folders offer no wildcards
        self.assertEqual(tracker.folders()['accepted'], 1)

    async def test_snapshots_exports_and_gap_sorted_wildcards(self):
        tracker.refresh_all()
        snapshot = tracker.create_snapshot('v0.4')
        self.assertEqual((snapshot['name'], snapshot['summary']['folders']), ('v0.4', 2))
        with self.assertRaises(ValueError):
            tracker.create_snapshot('v0.4')
        from app.services.collections import CollectionService
        CollectionService().remove_images(self.folder, [self.images['5']], self.root, 'ab' * 16)
        tracker.refresh_folder(self.folder)
        self.assertEqual(self.rows(snapshot_id=snapshot['id'])['alice']['delta'], -1)

        exported = list(csv.DictReader(io.StringIO(tracker.export_csv('character', snapshot['id']))))
        alice = next(row for row in exported if row['tag'] == 'alice')
        self.assertEqual((alice['now'], alice['delta'], alice['series']), ('1', '-1', 'wonderland'))
        name, text = tracker.export_snapshot_csv(snapshot['id'])
        self.assertEqual(name, 'v0.4')
        self.assertIn('character,danbooru,alice,2,2,0', text)

        # Wildcards: post 1 (alice) and post 3 (carol). Carol is a target with nothing in the dataset.
        tracker.set_goal('character', 'danbooru', ['alice', 'carol'], 3)
        items = curation.candidates(self.folder, sort='gaps')['items']
        self.assertEqual([i['remote_id'] for i in items], ['3', '1'])  # carol needs 3 of 3, alice 2 of 3
        self.assertEqual(items[0]['fills'], [{'tag': 'carol', 'now': 0, 'goal': 3, 'priority': False}])
        self.assertTrue(tracker.delete_snapshot(snapshot['id']))


if __name__ == '__main__':
    unittest.main()
