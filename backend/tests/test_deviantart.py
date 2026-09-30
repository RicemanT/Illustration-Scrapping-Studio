import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app.db as db
from app.models import FolderCreate
from app.providers.gallery_dl import GalleryDLProvider, _public_metadata
from app.providers.registry import supported_provider, provider_descriptors
from app.services.collections import CollectionService
from app.services.groups import create_group
from app.routes.providers import DeviantArtSettings, update_deviantart_config, gallery_dl_config
from app.services import provider_settings


def adapter():
    instance = object.__new__(GalleryDLProvider)
    instance.site = 'deviantart'
    instance.settings = {'pixiv_refresh_token': '', 'deviantart': {'refresh_token': 'test-token', 'client_id': 'test-id', 'client_secret': 'test-secret'}}
    return instance


class DeviantArtTests(unittest.IsolatedAsyncioTestCase):
    def test_urls_and_extractor_support(self):
        from gallery_dl import extractor
        instance = adapter()
        target = instance._target_url('artist:example-name')
        self.assertEqual(target, 'https://www.deviantart.com/example-name/gallery/all')
        self.assertIsNotNone(extractor.find(target))
        self.assertIsNotNone(extractor.find('https://www.deviantart.com/deviation/123456'))
        for value in ('https://www.deviantart.com/name/art/work-123', 'https://name.deviantart.com/gallery'):
            self.assertEqual(instance._target_url(value), value)
        for value in ('artist name', 'https://evil.test/name', 'https://deviantart.com.evil.test/name'):
            with self.assertRaises(ValueError): instance._target_url(value)

    async def test_metadata_lookup_and_pagination(self):
        instance = adapter()
        data = {'index': 987654321, 'username': 'Example', 'url': 'https://www.deviantart.com/example/art/work-987654321',
                'content': {'width': 2400, 'height': 3600}, 'tags': [{'tag_name': 'painting'}],
                'published_time': 1700000000, 'is_mature': True, 'extension': 'jpg', 'client_secret': 'private'}
        post = instance._normalize('https://images.test/work.jpg', data)
        self.assertEqual(post.remote_id, '987654321:1')
        self.assertEqual((post.width, post.height), (2400, 3600))
        self.assertEqual(post.tags['artist'], ['Example'])
        self.assertEqual(post.tags['general'], ['painting'])
        self.assertEqual(post.rating, 'questionable')
        self.assertNotIn('client_secret', post.raw_metadata)
        instance._post_cache.pop(('deviantart', post.remote_id), None)
        instance._run_gallery_dl = AsyncMock(return_value=[('https://images.test/work.jpg', data)])
        looked_up = await instance.get_post(post.remote_id)
        self.assertEqual(looked_up.remote_id, post.remote_id)
        instance._run_gallery_dl.assert_awaited_with('https://www.deviantart.com/deviation/987654321', 1, 100)
        posts, cursor = await instance.search('Example', limit=2)
        self.assertEqual(len(posts), 1)
        self.assertIsNone(cursor)
        with self.assertRaises(ValueError): await instance.search('Example', sort='oldest')
        instance._run_gallery_dl.side_effect = RuntimeError('authorization failed')
        with self.assertRaises(RuntimeError): await instance.search('Example')

    async def test_quota_stops_without_silent_quality_change(self):
        instance = adapter()
        instance._run_gallery_dl_once = AsyncMock(side_effect=RuntimeError("Free download limit reached."))
        with self.assertRaisesRegex(RuntimeError, 'site-side quota'):
            await instance._run_gallery_dl('target', 1, 20)
        self.assertEqual(instance._run_gallery_dl_once.await_count, 1)

    async def test_opt_in_quota_fallback_retries_same_range_once_and_marks_provenance(self):
        instance = adapter()
        instance.settings['deviantart_media_mode'] = 'prefer_original'
        instance._run_gallery_dl_once = AsyncMock(side_effect=[RuntimeError('Free download limit reached.'), [('https://image.test/full.jpg', {'index': 123})]])
        with patch('app.services.diagnostics.emit') as emit:
            result = await instance._run_gallery_dl('target', 21, 40)
            emit.assert_called_once()
        instance._run_gallery_dl_once.assert_awaited_with('target', 21, 40, original=False)
        self.assertEqual(result[0][1]['studio_media_selection'], 'published_after_original_quota')
        instance._run_gallery_dl_once = AsyncMock(side_effect=RuntimeError('HTTP 403 forbidden'))
        with self.assertRaisesRegex(RuntimeError, '403'):
            await instance._run_gallery_dl('target', 1, 20)
        self.assertEqual(instance._run_gallery_dl_once.await_count, 1)

    async def test_media_policy_does_not_clear_oauth_and_published_disables_download_endpoint(self):
        from app.routes.providers import update_deviantart_media, DeviantArtMedia
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider_settings, 'PROVIDER_SETTINGS_PATH', Path(temporary)/'settings.json'), patch('app.providers.gallery_dl.DEVIANTART_COOKIE_PATH', Path(temporary)/'cookies.txt'):
            await update_deviantart_config(DeviantArtSettings(refresh_token='test-token'))
            await update_deviantart_media(DeviantArtMedia(mode='published'))
            self.assertEqual(provider_settings.read_provider_settings()['deviantart']['refresh_token'], 'test-token')
            await update_deviantart_config(DeviantArtSettings(refresh_token='new-test-token'))
            self.assertEqual((await gallery_dl_config())['deviantart']['media_mode'], 'published')
        instance = adapter()
        instance.settings['deviantart_media_mode'] = 'published'
        self.assertFalse(instance._config()['extractor']['deviantart']['original'])

    def test_config_requests_originals_and_private_oauth(self):
        settings = adapter()._config()['extractor']['deviantart']
        self.assertTrue(settings['original'])
        self.assertFalse(settings['public'])
        self.assertFalse(settings['previews'])
        self.assertEqual(settings['refresh-token'], 'test-token')
        self.assertEqual(settings['client-secret'], 'test-secret')
        self.assertEqual(_public_metadata({'client_secret': 'private', 'refresh_token': 'private', 'title': 'ok'}), {'title': 'ok'})

    async def test_denied_media_has_actionable_error_and_removes_partial_file(self):
        import httpx
        from app.providers.booru import ProviderAuthenticationError
        instance = adapter()
        post = instance._normalize('https://image.test/file.jpg', {'index': 321, 'extension': 'jpg'})
        instance.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403, request=request)))
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory)/'image.jpg'
                with self.assertRaisesRegex(ProviderAuthenticationError, 'listing was accessible'):
                    await instance.download_image(post, str(target))
                self.assertFalse(target.exists())
        finally:
            await instance.client.aclose()

    def test_refresh_cache_survives_subprocesses_and_isolates_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch('app.providers.gallery_dl.LIBRARY_PATH', Path(directory)):
            instance = adapter()
            first = instance._cache_file('scratch-one')
            self.assertEqual(first, instance._cache_file('scratch-two'))
            self.assertTrue(first.is_file())
            instance.settings['deviantart'] = {}
            self.assertNotEqual(first, instance._cache_file('scratch-three'))

    async def test_credentials_flags_and_replacement(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider_settings, 'PROVIDER_SETTINGS_PATH', Path(temporary)/'settings.json'), patch('app.providers.gallery_dl.DEVIANTART_COOKIE_PATH', Path(temporary)/'cookies.txt'):
            await update_deviantart_config(DeviantArtSettings(refresh_token='test-token', client_id='test-id', client_secret='test-secret'))
            output = await gallery_dl_config()
            self.assertEqual(output['deviantart'], {'configured': True, 'custom_client_configured': True, 'media_mode': 'original', 'cookie_file_configured': False})
            self.assertNotIn('test-secret', str(output))
            await update_deviantart_config(DeviantArtSettings())
            self.assertFalse((await gallery_dl_config())['deviantart']['configured'])

    def test_retirement_preserves_collections_and_protection(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(db, 'DB_PATH', Path(temporary)/'index.db'):
            db.init_db()
            service = CollectionService()
            group = create_group('Legacy', 'danbooru', Path(temporary))
            folder = service.create_collection(FolderCreate(name='Artist', query='Artist', group_id=group['id'], sources=['danbooru']))
            conn = db.get_connection()
            conn.execute("UPDATE collection_source SET provider='removed-provider'")
            conn.execute("UPDATE artist_group SET provider='removed-provider'")
            conn.execute("INSERT INTO group_blocked_folder VALUES(?,?,?)", (group['id'], folder.id, 'now'))
            conn.commit(); conn.close()
            db.init_db(); db.init_db()
            self.assertEqual(service.get_collection(folder.id).slug, folder.slug)
            self.assertEqual(service.get_collection(folder.id).sources, [])
            conn = db.get_connection()
            self.assertEqual(conn.execute('SELECT provider FROM artist_group').fetchone()[0], 'retired')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM group_blocked_folder').fetchone()[0], 1)
            conn.close()
        self.assertTrue(supported_provider('deviantart'))
        descriptor = next(p for p in provider_descriptors() if p['name'] == 'deviantart')
        self.assertEqual(descriptor['collection_types'], ['artist'])


class ArtworkPageTests(unittest.IsolatedAsyncioTestCase):
    def test_candidates_are_scoped_ranked_and_opt_in(self):
        import json
        from app.providers.deviantart_page import page_candidates
        def item(index):
            return {"deviationId": index, "media": {"baseUri": "https://images.wixmp.com/f/image.png", "prettyName": "art", "token": ["signed"], "types": [
                {"t": "preview", "c": "/v1/fit/w_1024/art.jpg", "w": 1024, "h": 800},
                {"t": "fullview", "c": "/v1/fit/w_1600/<prettyName>.jpg", "w": 1600, "h": 1200},
                {"t": "square", "c": "/v1/crop/w_200/art.jpg", "w": 200, "h": 200},
                {"t": "blur", "c": "/v1/fit/blur_10/art.jpg", "w": 2000, "h": 2000},
                {"t": "bad", "c": "https://evil.test/image.jpg", "w": 9000, "h": 9000}]}}
        state = {"@@entities": {"deviation": {"other": item(999), "target": item(123)}}}
        html = "window.__INITIAL_STATE__ = JSON.parse(" + json.dumps(json.dumps(state)) + ");"
        self.assertEqual(len(page_candidates(html, 123)), 1)
        candidates = page_candidates(html, 123, True)
        self.assertEqual([kind for _, kind in candidates], ["fullview", "thumbnail"])
        self.assertIn("art.jpg?token=signed", candidates[0][0])
        self.assertEqual(page_candidates(html, 456, True), [])
        self.assertEqual(page_candidates('broken', 123, True), [])

    async def test_page_download_records_actual_image_and_thumbnail_fallback(self):
        import io, tempfile, httpx
        from pathlib import Path
        from PIL import Image
        output = io.BytesIO()
        Image.new('RGB', (64, 96)).save(output, format='JPEG')
        instance = adapter()
        instance.settings['deviantart_media_mode'] = 'published_preview'
        instance._deviantart_page_candidates = AsyncMock(return_value=[('https://images.wixmp.com/full.jpg', 'fullview'), ('https://images.wixmp.com/thumb.jpg', 'thumbnail')])
        calls = []
        def response(request):
            calls.append(request.url.path)
            return httpx.Response(200, content=output.getvalue()) if request.url.path == '/thumb.jpg' else (httpx.Response(200, content=b'not an image') if request.url.path == '/full.jpg' else httpx.Response(403))
        instance.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        post = instance._normalize('https://images.wixmp.com/api.jpg', {'index': 123, 'extension': 'jpg'})
        with tempfile.TemporaryDirectory() as directory:
            await instance.download_image(post, str(Path(directory) / 'image.jpg'))
        await instance.close()
        self.assertEqual(calls, ['/full.jpg', '/api.jpg', '/thumb.jpg'])
        self.assertEqual((post.width, post.height), (64, 96))
        self.assertEqual(post.raw_metadata['studio_media_selection'], 'thumbnail')
        self.assertFalse(post.raw_metadata['is_original'])


class DeviantArtCookieTests(unittest.TestCase):
    def test_private_filtered_cookie_storage_and_config(self):
        from app.providers import gallery_dl as module
        from http.cookiejar import MozillaCookieJar
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'DEVIANTART_COOKIE_PATH', Path(tmp)/'cookies.txt'):
            contents = '# Netscape HTTP Cookie File\n.deviantart.com\tTRUE\t/\tTRUE\t0\tauth_secure\tprivate-value\n.other.com\tTRUE\t/\tTRUE\t0\tother\tunrelated-secret\n'
            result = module.save_deviantart_cookie_file(contents)
            self.assertEqual(result['cookie_count'], 1)
            saved = module.DEVIANTART_COOKIE_PATH.read_text()
            self.assertNotIn('unrelated-secret', saved)
            jar = MozillaCookieJar(); jar.load(str(module.DEVIANTART_COOKIE_PATH), ignore_discard=True)
            self.assertEqual(next(iter(jar)).value, 'private-value')
            instance = adapter(); instance.settings['deviantart_cookie_file'] = str(module.DEVIANTART_COOKIE_PATH)
            self.assertEqual(instance._config()['extractor']['deviantart']['cookies'], str(module.DEVIANTART_COOKIE_PATH))
            with self.assertRaises(ValueError): module.save_deviantart_cookie_file('invalid')
            self.assertEqual(module.DEVIANTART_COOKIE_PATH.read_text(), saved)
            module.remove_deviantart_cookie_file()
            self.assertFalse(module.DEVIANTART_COOKIE_PATH.exists())


class DeviantArtPageSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_request_receives_cookie_jar(self):
        from app.providers import gallery_dl as module
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as tmp, patch.object(module, 'DEVIANTART_COOKIE_PATH', Path(tmp)/'cookies.txt'):
            module.save_deviantart_cookie_file('# Netscape HTTP Cookie File\n.deviantart.com\tTRUE\t/\tTRUE\t0\tauth_secure\tsession-value\n')
            instance = adapter(); instance.settings['deviantart_cookie_file'] = str(module.DEVIANTART_COOKIE_PATH)
            post = instance._normalize('https://images.wixmp.com/file.jpg', {'index': 123, 'extension': 'jpg'})
            session = MagicMock(); session.get.return_value.status_code = 200; session.get.return_value.text = 'no media'
            with patch.object(module.curl_requests, 'Session') as factory:
                factory.return_value.__enter__.return_value = session
                await instance._deviantart_page_candidates(post)
                cookies = list(factory.call_args.kwargs['cookies'])
                self.assertEqual(cookies[0].name, 'auth_secure')
                self.assertEqual(cookies[0].value, 'session-value')
                self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
