from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
import app.db as db
from app.services.runtime import LibraryLease, prepare_migration
from library_archive import backup, restore, digest

class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.library=self.root/'library';self.library.mkdir()
        self.old_db=db.DB_PATH;db.DB_PATH=self.library/'index.db';db.init_db()
        (self.library/'images').mkdir();(self.library/'images'/'pair.txt').write_text('actual tags')
        (self.library/'provider_settings.json').write_text('{"private":"kept"}')
    def tearDown(self):
        db.DB_PATH=self.old_db;self.temp.cleanup()
    def test_roundtrip_and_restore_disables_schedule(self):
        conn=db.get_connection();conn.execute('UPDATE sync_schedule SET enabled=1');conn.commit();conn.close()
        target=self.root/'backup.zip';backup(self.library,db.DB_PATH,target)
        restored=self.root/'restored';restore(target,restored)
        self.assertEqual((restored/'images'/'pair.txt').read_text(),'actual tags')
        self.assertEqual((restored/'provider_settings.json').read_text(),'{"private":"kept"}')
        with closing(sqlite3.connect(restored/'index.db')) as conn:self.assertEqual(conn.execute('SELECT enabled FROM sync_schedule').fetchone()[0],0)
        with self.assertRaises(ValueError):restore(target,restored)
    def test_busy_library_backup_rejected(self):
        with LibraryLease(self.library,db.DB_PATH):
            with self.assertRaises(RuntimeError):backup(self.library,db.DB_PATH,self.root/'busy.zip')
        self.assertFalse((self.root/'busy.zip').exists())
    def test_external_database_and_migration_snapshot(self):
        external=self.root/'external.db'
        from app.services.runtime import snapshot_database
        snapshot_database(db.DB_PATH,external)
        marker,version=prepare_migration(external);self.assertEqual(len(list((self.root/'migration-backups').iterdir())),1)
        marker.write_text(version);prepare_migration(external);self.assertEqual(len(list((self.root/'migration-backups').iterdir())),1)
        backup(self.library,external,self.root/'external.zip')
        restore(self.root/'external.zip',self.root/'restored')
    def test_corruption_and_traversal_leave_no_destination(self):
        archive=self.root/'bad.zip'
        for name in ['library/../escaped.txt','library/images/evil.txt']:
            with zipfile.ZipFile(archive,'w') as z:
                z.write(db.DB_PATH,'library/index.db');z.writestr(name,'corrupt')
                z.writestr('manifest.json',json.dumps({'format':'image-collection-studio-library','version':1,'files':{'library/index.db':{'size':db.DB_PATH.stat().st_size,'sha256':digest(db.DB_PATH)},name:{'size':7,'sha256':'wrong'}}}))
            with self.assertRaises(ValueError):restore(archive,self.root/'restored')
            self.assertFalse((self.root/'restored').exists());self.assertFalse((self.root/'escaped.txt').exists())
            self.assertFalse(list(self.root.glob('.collection-restore-*')))
    def test_backup_never_overwrites_and_rejects_nested_destination(self):
        target=self.root/'exists.zip';target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):backup(self.library,db.DB_PATH,target)
        self.assertEqual(target.read_bytes(),b'keep')
        with self.assertRaises(ValueError):backup(self.library,db.DB_PATH,self.library/'bad.zip')

    def test_empty_group_directories_are_restored(self):
        from app.services.groups import create_group
        group=create_group('Empty group','danbooru',self.library)
        target=self.root/'empty.zip';backup(self.library,db.DB_PATH,target)
        restore(target,self.root/'restored')
        self.assertTrue((self.root/'restored'/'images'/group['slug']).is_dir())
        self.assertTrue((self.root/'restored'/'thumbnails'/group['slug']).is_dir())
