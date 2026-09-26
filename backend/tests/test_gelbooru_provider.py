import unittest
from unittest.mock import patch

import httpx

from app.providers.booru import BooruProvider


class GelbooruProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_absolute_cursor_is_stable_when_requested_page_size_changes(self):
        ids = list(range(150, 0, -1))
        requested_pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("s") == "tag":
                return httpx.Response(200, json={"tag": []})
            pid = int(request.url.params.get("pid", "0"))
            requested_pages.append((pid, request.url.params.get("limit")))
            return httpx.Response(200, json={"post": [
                {"id": value, "file_url": f"https://img.example/{value}.jpg", "width": 800,
                 "height": 800, "tags": "", "rating": "s"}
                for value in ids[pid * 100:(pid + 1) * 100]
            ]})

        with patch("app.providers.booru.get_gelbooru_credentials", return_value=("123", "secret")):
            provider = BooruProvider("gelbooru")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def no_wait():
            return None

        provider._rate_limit = no_wait
        try:
            first, cursor = await provider.search("artist:test", limit=6)
            second, cursor = await provider.search("artist:test", cursor=cursor, limit=14)
            third, cursor = await provider.search("artist:test", cursor="o:95", limit=20)
        finally:
            await provider.close()
        self.assertEqual([int(post.remote_id) for post in first], ids[:6])
        self.assertEqual([int(post.remote_id) for post in second], ids[6:20])
        self.assertEqual([int(post.remote_id) for post in third], ids[95:115])
        self.assertEqual(cursor, "o:115")
        self.assertTrue(all(size == "100" for _, size in requested_pages))
        self.assertEqual(requested_pages[-2:], [(0, "100"), (1, "100")])

    async def test_search_adds_credentials_and_translates_artist_namespace(self):
        captured = {"post": {}, "tag": {}}

        def handler(request: httpx.Request) -> httpx.Response:
            operation = request.url.params.get("s")
            captured[operation].update(request.url.params)
            if operation == "tag":
                return httpx.Response(200, json={"tag": [
                    {"name": "namie-kun", "type": 1},
                    {"name": "blue_hair", "type": 0},
                ]})
            return httpx.Response(200, json={"post": [{
                "id": 42,
                "file_url": "https://img.example/42.jpg",
                "preview_url": "https://img.example/preview/42.jpg",
                "width": 800,
                "height": 1200,
                "tags": "namie-kun blue_hair",
                "rating": "s",
                "md5": "abc",
            }]})

        with patch("app.providers.booru.get_gelbooru_credentials", return_value=("123", "secret")):
            provider = BooruProvider("gelbooru")
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def no_wait():
            return None

        provider._rate_limit = no_wait
        try:
            posts, cursor = await provider.search("artist:namie-kun", limit=1)
        finally:
            await provider.close()

        self.assertEqual(captured["post"]["user_id"], "123")
        self.assertEqual(captured["post"]["api_key"], "secret")
        self.assertEqual(captured["post"]["tags"], "namie-kun")
        self.assertEqual(captured["tag"]["names"], "namie-kun blue_hair")
        self.assertEqual(posts[0].remote_id, "42")
        self.assertEqual(posts[0].tags["artist"], ["namie-kun"])
        self.assertEqual(posts[0].tags["general"], ["blue_hair"])
        self.assertEqual(cursor, "o:1")

    def test_xml_response_is_supported(self):
        response = httpx.Response(
            200,
            headers={"content-type": "application/xml"},
            text='<posts count="1"><post id="7" file_url="https://img.example/7.png" tags="test_tag" /></posts>',
        )
        posts = BooruProvider._response_posts(response)
        self.assertEqual(posts[0]["id"], "7")
        self.assertEqual(posts[0]["tags"], "test_tag")

    def test_tag_type_mapping_preserves_training_categories(self):
        provider = object.__new__(BooruProvider)
        provider.site = "gelbooru"
        tags = provider._parse_tags(
            {"tags": "pose series_name character_name highres"},
            {
                "pose": "general", "series_name": "copyright",
                "character_name": "character", "highres": "meta",
            },
        )
        self.assertEqual(tags, {
            "general": ["pose"], "copyright": ["series_name"],
            "character": ["character_name"], "meta": ["highres"],
        })


if __name__ == "__main__":
    unittest.main()
