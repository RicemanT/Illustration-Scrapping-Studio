import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from fastapi import HTTPException
import app.db as db
from app.models import FolderCreate, SyncResult
from app.routes import sync
from app.services.collections import CollectionService
from app.services.groups import create_group, import_artists, merge_groups, move_folder, recover_moves, parse_artists
from app.services.images import ImageService
from app.services.sync_jobs import get_job, create_job


class ArtistGroupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db = db.DB_PATH
        db.DB_PATH = self.root / 'index.db'
        db.init_db()
        self.service = CollectionService()
        self.dan = create_group('Danbooru', 'danbooru', self.root)
        self.e621 = create_group('E621', 'e621', self.root)
        sync._jobs.clear()
        sync._tasks.clear()

    async def asyncTearDown(self):
        tasks = list(sync._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sync._jobs.clear()
        sync._tasks.clear()
        db.DB_PATH = self.old_db
        self.temp.cleanup()

    async def test_protection_excludes_global_and_group_scraping(self):
        from app.routes.groups import add_blocked, ProtectionUpdate, remove_blocked
        first = self.folder('Curated', self.dan['id'])
        second = self.folder('New', self.dan['id'])
        add_blocked(self.dan['id'], ProtectionUpdate(text='\ufeffCURATED'))
        self.assertEqual([row[0] for row in sync._enabled_pairs()], [second.id])
        with patch.object(sync, '_start_new_job', side_effect=lambda job: job):
            job = await sync.sync_group(self.dan['id'])
        self.assertEqual([pair[0] for pair in job['parameters']['pairs']], [second.id])
        with self.assertRaises(HTTPException):
            add_blocked(self.dan['id'], ProtectionUpdate(text='New\nunknown'))
        self.assertEqual([row[0] for row in sync._enabled_pairs()], [second.id])
        remove_blocked(self.dan['id'], first.id)
        self.assertEqual(len(sync._enabled_pairs()), 2)

    async def test_delete_failure_retains_retryable_collection(self):
        folder = self.folder('Retry', self.dan['id'])
        directory = self.root / 'images' / folder.slug
        directory.mkdir(parents=True, exist_ok=True)
        with patch('app.services.collections.shutil.rmtree', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.service.delete_collection(folder.id, self.root)
        self.assertIsNotNone(self.service.get_collection(folder.id))
        self.assertTrue(self.service.delete_collection(folder.id, self.root))

    async def test_group_delete_purges_members_and_keeps_other_group(self):
        from app.services.groups import delete_group
        first = self.folder('Delete', self.dan['id'])
        other = self.folder('Keep', self.e621['id'])
        for folder in (first, other):
            for category in ('images', 'thumbnails'):
                path = self.root / category / folder.slug
                path.mkdir(parents=True, exist_ok=True)
                (path / 'fixture.txt').write_text('fixture')
        self.assertTrue(delete_group(self.dan['id'], self.root))
        self.assertIsNone(self.service.get_collection(first.id))
        self.assertTrue((self.root / 'images' / other.slug / 'fixture.txt').exists())
        self.assertFalse((self.root / 'images' / first.slug).exists())

    def folder(self, name='Artist', group=None, sources=None):
        return self.service.create_collection(FolderCreate(name=name, query=name, group_id=group, sources=sources or ['danbooru']))

    def image(self, folder, image_id=1):
        path = self.root / 'images' / folder.slug / f'{image_id}.jpg'
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (512, 512), 'red').save(path)
        path.with_suffix('.txt').write_text('red, artist', encoding='utf-8')
        thumb = self.root / 'thumbnails' / folder.slug / f'{image_id}_thumb.jpg'
        thumb.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 64), 'red').save(thumb)
        conn = db.get_connection()
        conn.execute("INSERT INTO image(id,folder_id,sha256,width,height,format,file_size,path,thumb_path,added_at) VALUES(?,?,?,512,512,'jpg',?,?,?,'now')",
                     (image_id, folder.id, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, f'{folder.slug}/{image_id}.jpg', f'{folder.slug}/{image_id}_thumb.jpg'))
        conn.execute("INSERT INTO collection_image(collection_id,image_id,added_at) VALUES(?,?,'now')", (folder.id, image_id))
        conn.commit(); conn.close()
        return path

    def test_bulk_preview_atomic_import_and_repeat_are_group_scoped(self):
        text = '\ufeff first_artist\r\n\nFirst Artist\nsecond\n'
        preview = import_artists(self.dan['id'], text)
        self.assertEqual(preview['counts']['new'], 2)
        self.assertEqual(preview['counts']['duplicate'], 1)
        self.assertEqual(self.service.list_collections(), [])
        created = import_artists(self.dan['id'], text, apply=True)
        self.assertEqual(created['counts']['created'], 2)
        repeated = import_artists(self.dan['id'], text, apply=True)
        self.assertEqual(repeated['counts']['created'], 0)
        self.assertEqual(repeated['counts']['existing'], 2)
        self.assertEqual(import_artists(self.e621['id'], text, apply=True)['counts']['created'], 2)
        folders = self.service.list_collections()
        self.assertEqual(len({folder.slug for folder in folders}), 4)
        for folder in folders:
            group = self.dan if folder.group_id == self.dan['id'] else self.e621
            self.assertTrue(folder.slug.startswith(group['slug'] + '/'))
            self.assertEqual([source['provider'] for source in folder.sources], [group['provider']])
        with self.assertRaises(ValueError):
            import_artists(self.dan['id'], 'new artist\nrating:explicit', apply=True)
        self.assertEqual(len(self.service.list_collections()), 4)
        with self.assertRaises(ValueError):
            parse_artists('a\n' * 5001)

    def test_bulk_transaction_rolls_back_on_creation_error(self):
        original = CollectionService.create_collection
        def create(service, data, **kwargs):
            if data.name == 'fail':
                raise ValueError('fixture failure')
            return original(service, data, **kwargs)
        with patch.object(CollectionService, 'create_collection', create), self.assertRaises(ValueError):
            import_artists(self.dan['id'], 'first\nfail', apply=True)
        self.assertEqual(self.service.list_collections(), [])

    def test_bulk_creates_real_artist_directories_and_enables_source_explicitly(self):
        from app.routes.groups import enable_group_source
        result = import_artists(self.dan['id'], 'Artist one\nArtist two', apply=True, library=self.root)
        for item in result['items']:
            folder = self.service.get_collection(item['folder_id'])
            for category in ('images', 'thumbnails'):
                self.assertTrue((self.root / category / folder.slug).is_dir())
        folder_id = result['items'][0]['folder_id']
        move_folder(folder_id, self.e621['id'], self.root)
        enable_group_source(self.e621['id'], folder_id)
        enable_group_source(self.e621['id'], folder_id)
        self.assertEqual({source['provider'] for source in self.service.get_collection(folder_id).sources}, {'danbooru', 'e621'})
        with self.assertRaises(HTTPException):
            enable_group_source(self.dan['id'], folder_id)

    async def test_group_history_remains_scoped_after_other_jobs(self):
        job = sync._new_job('all', 'manual', {'group_id': self.dan['id']})
        job['status'] = 'completed'
        create_job(job)
        for _ in range(25):
            other = sync._new_job('all', 'manual', {'group_id': self.e621['id']})
            other['status'] = 'completed'
            create_job(other)
        result = await sync.sync_history(1, self.dan['id'])
        self.assertEqual([item['job_id'] for item in result['items']], [job['job_id']])

    def test_disk_move_preserves_pairs_ids_and_deleted_image_recovery(self):
        folder = self.folder()
        original = self.image(folder)
        original_bytes = original.read_bytes()
        self.service.remove_images(folder.id, [1], self.root, 'abc123')
        move_folder(folder.id, self.dan['id'], self.root)
        moved = self.service.get_collection(folder.id)
        self.assertEqual(moved.group_id, self.dan['id'])
        self.assertTrue((self.root / 'images' / moved.slug).is_dir())
        self.assertEqual(self.service.restore_images(folder.id, 'abc123', self.root), 1)
        path = self.root / 'images' / moved.slug / '1.jpg'
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(path.with_suffix('.txt').read_text(), 'red, artist')
        self.assertTrue((self.root / 'thumbnails' / moved.slug / '1_thumb.jpg').is_file())
        move_folder(folder.id, self.e621['id'], self.root)
        moved_again = self.service.get_collection(folder.id)
        self.assertFalse(path.exists())
        self.assertEqual((self.root / 'images' / moved_again.slug / '1.jpg').read_bytes(), original_bytes)
        self.assertEqual(moved_again.image_count, 1)
        move_folder(folder.id, None, self.root)
        self.assertIsNone(self.service.get_collection(folder.id).group_id)
        self.assertTrue(original.exists())

    def test_interrupted_move_recovers_before_reconciliation(self):
        folder = self.folder()
        self.image(folder)
        real_rename = Path.rename
        def interrupted(source, target):
            if 'thumbnails' in source.parts:
                raise OSError('fixture interrupted')
            return real_rename(source, target)
        with patch.object(Path, 'rename', interrupted), self.assertRaises(OSError):
            move_folder(folder.id, self.dan['id'], self.root)
        self.assertEqual(self.service.get_collection(folder.id).slug, folder.slug)
        db.init_db()
        recover_moves(self.root)
        stats = ImageService(self.root).reconcile_filesystem()
        self.assertEqual(stats['collections'], 0)
        self.assertEqual(len(self.service.list_collections()), 1)
        moved = self.service.get_collection(folder.id)
        self.assertEqual(moved.image_count, 1)
        self.assertTrue((self.root / 'images' / moved.slug / '1.jpg').exists())
        conn = db.get_connection()
        self.assertEqual(conn.execute('SELECT count(*) FROM folder_move').fetchone()[0], 0)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        conn.close()

    def test_merge_groups_moves_collections_keeps_protection_and_removes_the_source(self):
        pilot = create_group('Pilot danbooru', 'danbooru', self.root)
        first = self.folder('Artist A', group=pilot['id'])
        second = self.folder('Artist B', group=pilot['id'])
        self.image(first)
        self.folder('Artist C', group=self.dan['id'])
        conn = db.get_connection()
        conn.execute("INSERT INTO group_blocked_folder(group_id, folder_id, created_at) VALUES (?, ?, 'x')", (pilot['id'], first.id))
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, 'same site'):
            merge_groups(pilot['id'], self.e621['id'], 'Mixed', self.root)
        result = merge_groups(pilot['id'], self.dan['id'], 'Danbooru full', self.root)
        self.assertEqual((result['moved'], result['group']['name']), (2, 'Danbooru full'))
        moved = self.service.get_collection(first.id)
        self.assertEqual((moved.group_id, self.service.get_collection(second.id).group_id), (self.dan['id'], self.dan['id']))
        self.assertTrue((self.root / 'images' / moved.slug / '1.jpg').is_file())
        conn = db.get_connection()
        protected = [tuple(r) for r in conn.execute('SELECT group_id, folder_id FROM group_blocked_folder')]
        source_left = conn.execute('SELECT count(*) FROM artist_group WHERE id=?', (pilot['id'],)).fetchone()[0]
        conn.close()
        self.assertEqual(protected, [(self.dan['id'], first.id)])
        self.assertEqual(source_left, 0)
        self.assertFalse((self.root / 'images' / pilot['slug']).exists())

    def test_merge_refuses_clashing_collection_names(self):
        other = create_group('Batch danbooru', 'danbooru', self.root)
        self.folder('Same Artist', group=other['id'])
        self.folder('Same Artist', group=self.dan['id'])
        with self.assertRaisesRegex(ValueError, 'Same Artist'):
            merge_groups(other['id'], self.dan['id'], 'Danbooru', self.root)

    def test_move_rejects_conflicts_and_active_jobs(self):
        folder = self.folder()
        self.folder(group=self.dan['id'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            move_folder(folder.id, self.dan['id'], self.root)
        job = sync._new_job('all', 'manual', {})
        create_job(job)
        with self.assertRaisesRegex(ValueError, 'pending'):
            move_folder(folder.id, self.e621['id'], self.root)
        self.assertIsNone(self.service.get_collection(folder.id).group_id)

    def test_grouped_reconciliation_never_creates_parent_artist(self):
        folder = self.folder(group=self.dan['id'])
        self.image(folder)
        for _ in range(2):
            ImageService(self.root).reconcile_filesystem()
        self.assertEqual(len(self.service.list_collections()), 1)
        self.assertEqual(self.service.get_collection(folder.id).image_count, 1)

    async def test_group_job_snapshot_and_restart_exclude_other_groups_and_sources(self):
        dan = self.folder('Same artist', self.dan['id'], ['danbooru', 'e621'])
        self.folder('Same artist', self.e621['id'], ['e621', 'danbooru'])
        self.folder('Ungrouped')
        # Capture persisted queued job, simulating restart before execution.
        with patch.object(sync, '_spawn', side_effect=lambda job: job):
            job = await sync.sync_group(self.dan['id'], limit=13, sort='oldest', date_from='2025-01-01')
        self.folder('Added after job start', self.dan['id'])
        calls = []
        async def fake(folder_id, provider, limit, **options):
            calls.append((folder_id, provider, limit, options['sort']))
            return SyncResult(folder_id=folder_id, provider=provider, new_images=2, skipped=0, errors=0, duration_seconds=0)
        with patch.object(sync.sync_service, 'sync_collection', side_effect=fake):
            self.assertEqual(await sync.resume_unfinished_jobs(), 1)
            await asyncio.gather(*list(sync._tasks.values()))
        self.assertEqual(calls, [(dan.id, 'danbooru', 13, 'oldest')])
        stored = get_job(job['job_id'])
        self.assertEqual(stored['status'], 'completed')
        self.assertEqual(stored['parameters']['group_id'], self.dan['id'])
        self.assertEqual(stored['result']['new_images'], 2)

    async def test_group_empty_invalid_dates_and_cancel(self):
        with self.assertRaises(HTTPException):
            await sync.sync_group(self.dan['id'])
        self.folder(group=self.dan['id'])
        with self.assertRaises(HTTPException):
            await sync.sync_group(self.dan['id'], date_from='not-a-date')
        started = asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        with patch.object(sync.sync_service, 'sync_collection', side_effect=slow):
            job = await sync.sync_group(self.dan['id'])
            await asyncio.wait_for(started.wait(), 2)
            task = sync._tasks[job['job_id']]
            await sync.cancel_sync_job(job['job_id'])
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(get_job(job['job_id'])['status'], 'canceled')


if __name__ == '__main__':
    unittest.main()
