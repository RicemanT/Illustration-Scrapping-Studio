import unittest
import tempfile
from pathlib import Path

import httpx

from app.providers.booru import BooruProvider


class BooruNormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_danbooru_and_e621_stop_keyset_at_short_page(self):
        for site in ("danbooru", "e621"):
            with self.subTest(site=site):
                if site == "danbooru":
                    payload = [{"id": value, "file_url": f"https://files.test/{value}.jpg",
                                "image_width": 800, "image_height": 800, "file_ext": "jpg"}
                               for value in (12, 11)]
                else:
                    payload = [{"id": value, "file": {"url": f"https://files.test/{value}.jpg",
                                    "width": 800, "height": 800, "ext": "jpg"}, "tags": {}}
                               for value in (12, 11)]

                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(200, json={"posts": payload} if site == "e621" else payload)

                provider = BooruProvider(site)
                await provider.client.aclose()
                provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

                async def no_wait():
                    return None

                provider._rate_limit = no_wait
                try:
                    posts, cursor = await provider.search("artist_name", limit=20)
                finally:
                    await provider.close()
                self.assertEqual(len(posts), 2)
                self.assertIsNone(cursor)

    async def test_streaming_download_reports_size_speed_and_completion(self):
        payload = b"x" * (2 * 1024 * 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-length": str(len(payload))},
                content=payload,
            )

        provider = BooruProvider("danbooru")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def no_wait():
            return None

        provider._rate_limit = no_wait
        post = provider._post_to_remote({
            "id": 10,
            "file_url": "https://files.example/10.bin",
            "image_width": 512,
            "image_height": 512,
            "file_ext": "bin",
            "tag_string_general": "test",
        })
        updates = []
        try:
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "download.bin"
                await provider.download_image(post, str(target), progress=updates.append)
                self.assertEqual(target.read_bytes(), payload)
        finally:
            await provider.close()

        self.assertGreaterEqual(len(updates), 2)
        self.assertEqual(updates[-1]["stage"], "complete")
        self.assertEqual(updates[-1]["bytes_downloaded"], len(payload))
        self.assertEqual(updates[-1]["bytes_total"], len(payload))
        self.assertEqual(updates[-1]["percent"], 100)
        self.assertGreater(updates[-1]["speed_bps"], 0)
        self.assertEqual(updates[-1]["eta_seconds"], 0)
        self.assertGreater(updates[-1]["elapsed_seconds"], 0)

    async def test_danbooru_preserves_categories_and_preview(self):
        provider = BooruProvider("danbooru")
        try:
            post = provider._post_to_remote({
                "id": 11,
                "file_url": "https://cdn.example/11.png",
                "preview_file_url": "https://cdn.example/preview/11.jpg",
                "image_width": 1200,
                "image_height": 800,
                "file_ext": "png",
                "md5": "abc",
                "rating": "s",
                "tag_string_artist": "artist_name",
                "tag_string_character": "character_name",
                "tag_string_copyright": "series_name",
                "tag_string_general": "blue_hair solo",
                "tag_string_meta": "highres",
            })
        finally:
            await provider.close()
        self.assertEqual(post.preview_url, "https://cdn.example/preview/11.jpg")
        self.assertEqual((post.width, post.height), (1200, 800))
        self.assertEqual(post.rating, "safe")
        self.assertEqual(post.tags["artist"], ["artist_name"])
        self.assertEqual(post.tags["meta"], ["highres"])

    async def test_e621_preserves_original_video_and_static_preview_urls(self):
        provider = BooruProvider("e621")
        try:
            post = provider._post_to_remote({
                "id": 22,
                "file": {
                    "url": "https://static.example/22.webm",
                    "width": 1920,
                    "height": 1080,
                    "ext": "webm",
                    "md5": "video-md5",
                },
                "preview": {"url": "https://static.example/preview/22.jpg"},
                "sample": {"url": "https://static.example/sample/22.jpg"},
                "tags": {
                    "artist": ["artist_name"],
                    "character": ["character_name"],
                    "copyright": ["series_name"],
                    "species": ["human", "canine"],
                    "general": ["animated"],
                    "meta": ["webm"],
                },
                "rating": "e",
                "score": {"total": 42},
                "sources": ["https://artist.example/work/22"],
            })
        finally:
            await provider.close()
        self.assertEqual(post.image_url, "https://static.example/22.webm")
        self.assertEqual(post.preview_url, "https://static.example/preview/22.jpg")
        self.assertEqual(post.format, "webm")
        self.assertEqual(post.md5, "video-md5")
        self.assertEqual(post.rating, "explicit")
        self.assertEqual(post.score, 42)
        self.assertEqual(post.source, "https://artist.example/work/22")
        self.assertEqual(post.tags["species"], ["human", "canine"])



    async def test_slow_downloads_overlap_with_spaced_starts(self):
        import asyncio
        import time
        starts, updates = [], []
        both_started, release = asyncio.Event(), asyncio.Event()
        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                await release.wait()
                yield b'test'
        def handler(request):
            starts.append(time.monotonic())
            if len(starts) == 2: both_started.set()
            return httpx.Response(200, stream=SlowBody())
        provider = BooruProvider('danbooru')
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider.config = dict(provider.config, download_interval=0.05)
        tasks = []
        try:
            with tempfile.TemporaryDirectory() as temp:
                for ident in (1, 2):
                    post = provider._post_to_remote({'id': ident, 'file_url': f'https://files.example/{ident}.jpg', 'image_width': 512, 'image_height': 512, 'file_ext': 'jpg'})
                    tasks.append(asyncio.create_task(provider.download_image(post, str(Path(temp)/f'{ident}.jpg'), progress=updates.append)))
                try:
                    await asyncio.wait_for(both_started.wait(), timeout=3)
                    self.assertFalse(any(task.done() for task in tasks))
                    self.assertGreaterEqual(starts[1]-starts[0], 0.045)
                    self.assertEqual(len([u for u in updates if u['stage']=='waiting_for_provider']), 2)
                finally:
                    release.set()
                    await asyncio.gather(*tasks)
                for ident in (1, 2): self.assertEqual((Path(temp)/f'{ident}.jpg').read_bytes(), b'test')
        finally:
            await provider.close()


if __name__ == "__main__":
    unittest.main()
