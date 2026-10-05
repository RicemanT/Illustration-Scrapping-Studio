import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.providers import pacing
from app.providers import booru
from app.providers.booru import BooruProvider


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_waits_get_distinct_spaced_slots(self):
        pacer = pacing.Pacer()
        starts = []

        async def one():
            await pacer.wait(0.05)
            starts.append(time.monotonic())

        await asyncio.gather(*(one() for _ in range(4)))
        gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
        self.assertTrue(all(gap >= 0.04 for gap in gaps), gaps)

    async def test_throttle_backs_off_and_success_recovers(self):
        pacer = pacing.Pacer()
        self.assertEqual(pacer.throttled(3.0, 1.0), 3.0)
        self.assertEqual(pacer.backoff, 2.0)
        self.assertGreater(pacer.next_at, time.monotonic() + 2.5)
        for _ in range(10):
            pacer.succeeded()
        self.assertLess(pacer.backoff, 1.0 * 2)
        self.assertGreaterEqual(pacer.backoff, 1.0)

    def test_saved_intervals_are_clamped_to_documented_limits(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch('app.services.provider_settings.PROVIDER_SETTINGS_PATH', Path(temp) / 'settings.json'):
            saved = pacing.save({'danbooru': {'api': 0.01, 'download': 0}, 'e621': {'api': 0.1, 'download': 'fast'}})
            self.assertEqual(saved['danbooru'], {'api': 0.15, 'download': 0.0})
            self.assertEqual(saved['e621'], {'api': 0.55, 'download': pacing.DEFAULTS['e621']['download']})
            self.assertEqual(saved['gelbooru'], pacing.DEFAULTS['gelbooru'])


class ProviderPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_retries_after_429_and_slows_the_site(self):
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, content=b'image')

        provider = BooruProvider('danbooru')
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider.config = dict(provider.config, download_interval=0)
        pacing.pacer('danbooru', 'download').backoff = 1.0
        try:
            post = provider._post_to_remote({'id': 5, 'file_url': 'https://cdn.example/5.jpg', 'image_width': 600, 'image_height': 800, 'file_ext': 'jpg'})
            with tempfile.TemporaryDirectory() as temp:
                await provider.download_image(post, str(Path(temp) / '5.jpg'))
                self.assertEqual((Path(temp) / '5.jpg').read_bytes(), b'image')
            self.assertEqual(len(calls), 2)
            self.assertGreater(pacing.pacer('danbooru', 'download').backoff, 1.0)
        finally:
            pacing.pacer('danbooru', 'download').backoff = 1.0
            await provider.close()

    async def test_account_identifies_on_api_requests_but_not_file_downloads(self):
        seen = []

        def handler(request):
            seen.append((request.url.host, request.headers.get('authorization'), request.headers.get('user-agent')))
            if request.url.host == 'danbooru.donmai.us':
                return httpx.Response(200, json=[])
            return httpx.Response(200, content=b'x')

        with patch.object(booru, 'get_account', return_value={'login': 'artist_fan', 'api_key': 'secret', 'user_id': 1234}):
            provider = BooruProvider('danbooru')
        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=provider.client.headers,
                                             auth=provider.client.auth)
        await provider.client.aclose()
        provider.client = transport_client
        provider._rate_limit = lambda: asyncio.sleep(0)
        provider.config = dict(provider.config, download_interval=0)
        try:
            provider.literal_query = True
            await provider.search('some_artist', None, 20)
            post = provider._post_to_remote({'id': 7, 'file_url': 'https://cdn.donmai.us/7.jpg', 'image_width': 600, 'image_height': 800, 'file_ext': 'jpg'})
            with tempfile.TemporaryDirectory() as temp:
                await provider.download_image(post, str(Path(temp) / '7.jpg'))
        finally:
            await provider.close()
        api, cdn = seen
        self.assertEqual(api[0], 'danbooru.donmai.us')
        self.assertTrue(api[1].startswith('Basic '))
        self.assertIn('user #1234', api[2])
        self.assertEqual(cdn[0], 'cdn.donmai.us')
        self.assertIsNone(cdn[1])

    def test_user_agent_names_the_account_or_the_project(self):
        self.assertIn('user #9', booru.user_agent('danbooru', {'user_id': 9}))
        self.assertIn('by someone on e621', booru.user_agent('e621', {'login': 'someone'}))
        self.assertNotIn('example.com', booru.user_agent('e621', {}))


if __name__ == '__main__':
    unittest.main()
