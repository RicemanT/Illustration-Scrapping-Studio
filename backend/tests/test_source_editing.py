import asyncio
import tempfile
import unittest
from pathlib import Path
from fastapi import HTTPException
import app.db as db
from app.models import FolderCreate
from app.services.collections import CollectionService
from app.services.groups import create_group, import_artists
from app.routes.collections import update_collection_source

class SourceEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.old=db.DB_PATH;db.DB_PATH=self.root/'index.db';db.init_db()
        self.service=CollectionService()
        self.folder=self.service.create_collection(FolderCreate(name='Artist',query='artist',sources=['danbooru']))
    def tearDown(self):
        db.DB_PATH=self.old;self.temp.cleanup()
    def update(self, provider, data):
        return asyncio.run(update_collection_source(self.folder.id,provider,data))
    def test_add_disable_reenable_preserves_query_and_cursor(self):
        self.update('e621',{'enabled':True,'query_override':'different_artist'})
        conn=db.get_connection();conn.execute("UPDATE collection_source SET last_cursor='123',backfill_cursor='456' WHERE provider='e621'");conn.commit();conn.close()
        self.update('e621',{'enabled':False});self.update('e621',{'enabled':True})
        sources=self.service.get_collection(self.folder.id).sources
        self.assertEqual(len(sources),2)
        extra=next(s for s in sources if s['provider']=='e621')
        self.assertEqual(extra['query_override'],'different_artist');self.assertEqual(extra['last_cursor'],'123')
        self.update('e621',{'query_override':'new_artist'})
        conn=db.get_connection();row=conn.execute("SELECT last_cursor,backfill_cursor FROM collection_source WHERE provider='e621'").fetchone();conn.close()
        self.assertEqual(tuple(row),(None,None))
    def test_invalid_source_does_not_change_state(self):
        for provider,payload in [('nonsense',{'enabled':True}),('e621',{'enabled':True,'query_override':'bad\nquery'}),('e621',{'query_override':123})]:
            with self.assertRaises(HTTPException):self.update(provider,payload)
        self.assertEqual(len(self.service.get_collection(self.folder.id).sources),1)
    def test_character_rejects_profile_source(self):
        self.folder=self.service.create_collection(FolderCreate(name='Character',type='character',query='character'))
        with self.assertRaises(HTTPException):self.update('pixiv',{'enabled':True})
    def test_bulk_creates_multiple_sources_and_keeps_existing(self):
        group=create_group('Group','danbooru',self.root)
        import_artists(group['id'],'first',apply=True,library=self.root)
        result=import_artists(group['id'],'first\nsecond',apply=True,library=self.root,additional_sources=['e621','e621','pixiv'])
        for item in result['items']:
            folder=self.service.get_collection(item['folder_id'])
            self.assertEqual(len(folder.sources),1 if item['name']=='first' else 3)
        with self.assertRaises(ValueError):import_artists(group['id'],'bad',apply=True,kind='character',additional_sources=['pixiv'])
