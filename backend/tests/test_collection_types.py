import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
import app.db as db
from app.models import FolderCreate, ImportPreviewRequest
from app.services.collections import CollectionService
from app.services.groups import create_group, import_artists
from app.services.queries import provider_query_for_folder
from app.services.tags import TagService
from app.routes import imports
import test_tags

class CollectionTypeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.old_db = db.DB_PATH; db.DB_PATH = self.root / 'index.db'; db.init_db()
        self.service = CollectionService()
    def tearDown(self):
        db.DB_PATH = self.old_db; self.temp.cleanup()
    def test_character_identity_and_raw_tag_queries(self):
        self.assertEqual(provider_query_for_folder('hatsune miku', 'character'), 'hatsune_miku')
        raw = 'character:hatsune_miku solo -comic rating:safe'
        self.assertEqual(provider_query_for_folder(raw, 'tag'), raw)
    def test_types_overrides_and_profile_rejection(self):
        folder = self.service.create_collection(FolderCreate(name='Miku pictures', type='character', query='hatsune miku', source_queries={'danbooru':'hatsune_miku'}))
        self.assertIsNone(folder.artist_tag_template)
        self.assertEqual(self.service.get_collection(folder.id).sources[0]['query_override'], 'hatsune_miku')
        with self.assertRaises(ValidationError): FolderCreate(name='Miku', type='character', query='miku', sources=['artstation'])
        with self.assertRaises(ValidationError): FolderCreate(name='x', type='typo', query='x')
    def test_bulk_types_are_independent_and_repeat_safe(self):
        group = create_group('Mixed', 'danbooru', self.root)
        artist = import_artists(group['id'], 'hatsune_miku', apply=True)
        char = import_artists(group['id'], 'hatsune_miku\nHatsune Miku', kind='character', apply=True)
        self.assertEqual(char['counts']['created'], 1); self.assertEqual(char['counts']['duplicate'], 1)
        self.assertNotEqual(char['items'][0]['folder_id'], artist['items'][0]['folder_id'])
        self.assertEqual(import_artists(group['id'], 'hatsune miku', kind='character', apply=True)['counts']['existing'], 1)
        tag = import_artists(group['id'], 'blue_hair solo\nlandscape -comic', kind='tag', apply=True)
        self.assertEqual(tag['counts']['created'], 2)
        self.assertEqual(self.service.get_collection(tag['items'][0]['folder_id']).query, 'blue_hair solo')
    def test_invalid_bulk_is_atomic_and_query_edits_reset_cursors(self):
        group = create_group('Characters', 'danbooru', self.root)
        report = import_artists(group['id'], 'rating:safe\nmiku', kind='character')
        self.assertEqual(report['counts']['invalid'], 1)
        with self.assertRaises(ValueError): import_artists(group['id'], 'miku\nrating:safe', kind='character', apply=True)
        self.assertEqual(self.service.list_collections(), [])
        folder = self.service.create_collection(FolderCreate(name='Landscape', type='tag', query='landscape'))
        conn=db.get_connection(); conn.execute("UPDATE collection_source SET last_cursor='99',backfill_cursor='88'"); conn.commit(); conn.close()
        self.service.update_collection(folder.id, {'query':'landscape sunset'})
        self.assertIsNone(self.service.get_collection(folder.id).sources[0]['last_cursor'])
        with self.assertRaises(ValueError): self.service.update_collection(folder.id, {'type':'artist'})
    def test_reinitialization_preserves_existing_collections(self):
        artist = self.service.create_collection(FolderCreate(name='Existing Artist', query='existing artist'))
        db.init_db()
        result = self.service.get_collection(artist.id)
        self.assertEqual((result.slug,result.type,result.artist_tag_template), (artist.slug,'artist','Drawn by {artist}'))

class NonArtistCreditTests(unittest.TestCase):
    setUp = test_tags.TagServiceTests.setUp
    tearDown = test_tags.TagServiceTests.tearDown
    def test_character_retains_actual_artist_without_folder_trigger(self):
        conn=db.get_connection()
        conn.execute("UPDATE collection SET type='character',name='Miku',artist_tag_template='Drawn by {artist}'")
        source=conn.execute('SELECT id FROM image_source WHERE image_id=1').fetchone()[0]
        conn.execute("INSERT INTO image_tag(image_id,source_id,category,tag) VALUES(1,?,'artist','real_artist')",(source,));conn.commit()
        tags=TagService._source_tags(conn,1)
        self.assertIn('real artist',tags);self.assertNotIn('Drawn by miku',tags)
        self.assertEqual(TagService._source_category_map(conn,1,['artist'])['real artist'],'artist')
        conn.close()


class LiteralProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_tag_namespace_does_not_swallow_following_tags(self):
        import httpx
        from app.providers.booru import BooruProvider
        seen=[]
        def handler(request):
            seen.append(request.url.params['tags'])
            return httpx.Response(200,json=[])
        provider=BooruProvider('danbooru')
        await provider.client.aclose()
        provider.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider._rate_limit=AsyncMock()
        provider.literal_query=True
        try: await provider.search('character:hatsune_miku solo -comic',limit=1)
        finally: await provider.close()
        self.assertIn('character:hatsune_miku solo -comic',seen[0])
