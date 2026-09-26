import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from fastapi import FastAPI

import test_local_filters
import test_qa_export
from app.routes import collections, dataset, imports
from app.models import ImportPreviewRequest


class PhaseSevenAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = test_local_filters.LocalFilterTests.setUp
    tearDown = test_local_filters.LocalFilterTests.tearDown

    async def test_shared_query_preview_validation_and_explicit_confirmation(self):
        app = FastAPI()
        app.include_router(collections.router,prefix='/api/folders')
        app.include_router(dataset.router,prefix='/api/dataset')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response = await client.post('/api/folders/1/images/query',json={'filters':{'required_tags':['solo'],'excluded_tags':['red fox']},'limit':1})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['total'],1)
            self.assertEqual(response.json()['items'][0]['id'],2)
            self.assertEqual((await client.post('/api/folders/1/images/query',json={'limit':100000})).status_code,422)
            response = await client.post('/api/dataset/folders/1/preview',json={'filters':{'favorite':True}})
            token = response.json()['preview_token']
            payload={'preview_token':token,'image_ids':[2],'status':'rejected'}
            self.assertEqual((await client.post('/api/dataset/folders/1/review',json=payload)).status_code,422)
            response = await client.post('/api/dataset/folders/1/review',json={**payload,'confirmed':True})
            self.assertEqual(response.status_code,200)
            undo=response.json()['undo_token']
            self.assertEqual((await client.post(f'/api/dataset/folders/1/review/undo/{undo}')).json()['restored'],1)

    async def test_preview_disconnect_cancels_search_and_closes_provider(self):
        provider=AsyncMock()
        async def pending(*args,**kwargs):
            await asyncio.sleep(30)
        provider.search.side_effect=pending
        request=AsyncMock()
        request.is_disconnected.return_value=True
        with patch.object(imports,'create_provider',return_value=provider):
            with self.assertRaises(asyncio.CancelledError):
                await imports.import_preview(ImportPreviewRequest(folder_id=1,provider='danbooru',query='artist'),request)
        provider.close.assert_awaited_once()


class StartupPreservationTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_qa_export.DatasetQAExportTests.setUp
    tearDown = test_qa_export.DatasetQAExportTests.tearDown

    async def test_startup_leaves_stale_sidecar_for_explicit_qa_repair(self):
        from app import main
        from app.routes import sync
        sidecar = self.image_path.with_suffix('.txt')
        sidecar.write_text('stale sidecar retained for review',encoding='utf-8')
        before = self.image_path.read_bytes()
        with patch.object(main,'LIBRARY_PATH',self.root), patch.object(sync,'start_background_services',AsyncMock(return_value=0)):
            await main.startup_event()
        self.assertEqual(sidecar.read_text(encoding='utf-8'),'stale sidecar retained for review')
        self.assertEqual(self.image_path.read_bytes(),before)
        self.assertIn('sidecar_mismatch',self.service.validate_collection(1)['summary']['by_code'])
