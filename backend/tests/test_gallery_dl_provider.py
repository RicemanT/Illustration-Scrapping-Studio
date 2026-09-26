import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import numpy as np

from app.providers.gallery_dl import (
    ARTSTATION_COVER_FILTER,
    GalleryDLProvider,
    _artstation_asset_identity,
    _artstation_cover_tier,
    _artstation_original_tiers,
    _artstation_slug,
    _parse_artstation_portfolio,
    _parse_artstation_project,
    _public_metadata,
    _select_artstation_cover_messages,
    save_twitter_cookie_file,
)


def provider(site: str) -> GalleryDLProvider:
    instance = object.__new__(GalleryDLProvider)
    instance.site = site
    instance.settings = {
        "pixiv_refresh_token": "", "twitter_auth_mode": "",
        "twitter_browser": "", "twitter_profile": "", "twitter_cookie_file": "",
    }
    return instance


class GalleryDLProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_dump_json_decoder_accepts_nested_and_line_messages(self):
        nested = json.dumps([[2, "", {}], [3, "https://cdn.test/a.jpg", {"id": 4}]])
        self.assertEqual(GalleryDLProvider._decode_messages(nested)[0][0], "https://cdn.test/a.jpg")
        lines = '\n'.join((json.dumps([2, "", {}]), json.dumps([3, "https://cdn.test/b.png", {"id": 5}])))
        self.assertEqual(GalleryDLProvider._decode_messages(lines)[0][1]["id"], 5)

    def test_embedded_gallery_dl_errors_are_not_silently_treated_as_empty(self):
        output = json.dumps([[-1, {"error": "AuthenticationError", "message": "refresh token rejected"}]])
        self.assertEqual(GalleryDLProvider._decode_errors(output), ["refresh token rejected"])

    def test_pixiv_ugoira_keeps_original_archive_and_all_metadata_categories(self):
        item = provider("pixiv")._normalize("https://i.pximg.net/ugoira/123.zip", {
            "id": 123, "num": 0, "extension": "zip", "width": 1200, "height": 1600,
            "user": {"name": "Artist Name", "account": "artist"},
            "tags": [{"name": "Tag One"}, {"tag": "Tag Two"}], "rating": "R-18",
            "date": "2026-01-02T03:04:05+00:00", "_http_headers": {"Referer": "secret"},
            "refresh_token": "must-not-leak",
        })
        self.assertEqual(item.remote_id, "123:1")
        self.assertEqual(item.format, "zip")
        self.assertEqual(item.tags["artist"], ["Artist Name"])
        self.assertEqual(item.tags["meta"], ["animated"])
        self.assertNotIn("_http_headers", item.raw_metadata)
        self.assertNotIn("refresh_token", item.raw_metadata)

    def test_multi_asset_ids_are_stable_per_provider(self):
        art = provider("artstation")._normalize("https://cdn.test/image.jpg", {
            "id": 22, "hash_id": "AbCd", "num": 2, "extension": "jpg",
            "asset": {"id": 91, "width": 3000, "height": 2000},
            "userinfo": {"username": "maker"},
        })
        paw = provider("pawchive")._normalize("https://file.pawchive.pw/data/a.webm", {
            "id": "777", "num": 3, "service": "patreon", "user": "42",
            "username": "Maker", "extension": "webm",
        })
        self.assertEqual(art.remote_id, "AbCd:91")
        self.assertEqual(art.width, 3000)
        self.assertEqual(paw.remote_id, "patreon:42:777:3")
        self.assertEqual(paw.tags["meta"], ["video"])

    async def test_pawchive_keeps_paired_gif_and_video_originals(self):
        instance = provider("pawchive")
        common = {
            "id": "777", "service": "patreon", "user": "42",
            "file": {"name": "preview.gif", "extension": "gif", "original": True,
                     "url": "https://file.pawchive.pw/preview.gif"},
            "attachments": [
                {"name": "finished.mp4", "extension": "mp4", "original": True,
                 "url": "https://file.pawchive.pw/finished.mp4"},
            ],
        }
        instance._run_gallery_dl = AsyncMock(return_value=[
            ("https://file.pawchive.pw/preview.gif", {**common, "num": 1, "extension": "gif"}),
            ("https://file.pawchive.pw/finished.mp4", {**common, "num": 2, "extension": "mp4"}),
        ])

        posts, cursor = await instance.search("https://pawchive.pw/patreon/user/42", limit=20)

        self.assertEqual([post.format for post in posts], ["gif", "mp4"])
        self.assertIsNone(cursor)
        self.assertNotIn("image-filter", instance._config()["extractor"]["pawchive"])

    def test_artstation_cover_path_matches_full_resolution_asset(self):
        cover = "https://cdna.artstation.com/p/assets/images/images/123/456/789/medium/finished-piece.jpg?1"
        original = "https://cdnb.artstation.com/p/assets/images/images/123/456/789/8k/finished-piece.jpg?2"
        self.assertEqual(_artstation_asset_identity(cover), _artstation_asset_identity(original))

        from gallery_dl.util import compile_filter
        matches = compile_filter(ARTSTATION_COVER_FILTER)
        self.assertTrue(matches({"cover_url": cover, "asset": {"image_url": original}}))
        self.assertFalse(matches({
            "cover_url": cover,
            "asset": {"image_url": "https://cdna.artstation.com/p/assets/images/images/999/888/777/large/sketch.jpg"},
        }))

    def test_artstation_portfolio_parser_and_cover_tier(self):
        cover = "https://cdna.artstation.com/p/assets/covers/images/053/913/106/smaller_square/finished.jpg?1663312457"
        html = (
            '<a href="/projects/Are9Re"><img src="' + cover + '">'
            '<div class="album-grid-item-name"> Finished piece </div></a>'
        )
        self.assertEqual(_artstation_slug("https://www.artstation.com/braveking"), "braveking")
        self.assertEqual(_artstation_slug("https://braveking.artstation.com/projects"), "braveking")
        self.assertEqual(_artstation_cover_tier(cover, "original"), cover.replace("/smaller_square/", "/original/"))
        self.assertEqual(_parse_artstation_portfolio(html), [{
            "hash_id": "Are9Re", "cover_url": cover, "title": "Finished piece",
        }])
        project_html = (
            '<a class="colorbox-gal" href="https://cdn.test/assets/images/images/1/2/3/large/final.jpg">'
            '<img src="https://cdn.test/assets/images/images/1/2/3/medium/final.jpg"></a>'
            '<video><source src="https://cdn.test/final.mp4"></video>'
        )
        self.assertEqual(_parse_artstation_project(project_html), (
            ["https://cdn.test/assets/images/images/1/2/3/large/final.jpg"],
            ["https://cdn.test/final.mp4"],
        ))
        self.assertEqual(_artstation_original_tiers(
            "https://cdn.test/assets/images/images/1/2/3/large/final.jpg"
        )[0], "https://cdn.test/assets/images/images/1/2/3/8k/final.jpg")

    async def test_artstation_connection_reset_uses_real_project_asset_not_cover(self):
        instance = provider("artstation")
        cover = "https://cdna.artstation.com/p/assets/covers/images/053/913/106/smaller_square/finished.jpg?1663312457"
        original = "https://cdnb.artstation.com/p/assets/images/images/053/913/101/8k/finished.jpg?1663312447"
        instance._run_gallery_dl = AsyncMock(side_effect=RuntimeError("ConnectionResetError(10054)"))
        instance._artstation_portfolio_fallback = AsyncMock(return_value=[(original, {
            "id": "Are9Re",
            "hash_id": "Are9Re",
            "title": "Finished piece",
            "extension": "jpg",
            "date": "2026-01-01T00:00:00+00:00",
            "cover_url": cover,
            "userinfo": {"username": "fallback-artist"},
            "asset": {"id": "53913101", "image_url": original, "width": 1600, "height": 1351},
            "artstation_selection": {
                "method": "portfolio_project_visual_match",
                "project_id": "Are9Re",
                "candidate_count": 6,
                "selected_asset_id": "53913101",
                "visual_match_score": 26.8,
            },
        })])
        GalleryDLProvider._artstation_portfolio_until.pop("fallback-artist", None)

        posts, cursor = await instance.search("https://www.artstation.com/fallback-artist", limit=10)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].remote_id, "Are9Re:53913101")
        self.assertIn("/images/images/", posts[0].image_url)
        self.assertNotIn("/assets/covers/", posts[0].image_url)
        self.assertEqual(posts[0].preview_url, cover)
        self.assertEqual(posts[0].raw_metadata["artstation_selection"]["method"], "portfolio_project_visual_match")
        self.assertIsNone(cursor)

    async def test_artstation_fallback_reports_parallel_project_discovery_in_portfolio_order(self):
        instance = provider("artstation")
        progress = []
        instance.search_progress = progress.append
        projects = [
            {"hash_id": value, "cover_url": f"https://cdn.test/{value}.jpg", "title": f"Project {value}"}
            for value in ("first", "second", "third")
        ]

        def resolve(_slug, _portfolio_url, project):
            project_id = project["hash_id"]
            return f"https://cdn.test/{project_id}.jpg", {
                "hash_id": project_id,
                "artstation_selection": {"visual_match_score": 1.5},
            }

        with patch.object(GalleryDLProvider, "_artstation_load_portfolio", return_value=projects), \
             patch.object(GalleryDLProvider, "_artstation_resolve_project", side_effect=resolve) as resolver, \
             patch("app.providers.gallery_dl.get_parallel_workers", return_value=4):
            messages = await instance._artstation_portfolio_fallback("artist", 0, 3)

        self.assertEqual([data["hash_id"] for _, data in messages], ["first", "second", "third"])
        self.assertEqual(resolver.call_count, 3)
        self.assertEqual(progress[0]["workers"], 3)
        self.assertEqual(progress[0]["total"], 3)
        self.assertEqual(progress[-1]["completed"], 3)
        self.assertIsInstance(progress[-1]["elapsed_seconds"], float)
        self.assertEqual(progress[-1]["eta_seconds"], 0)
        self.assertIn("matched 3/3", progress[-1]["message"])

    def test_artstation_forbidden_asset_stops_after_one_quick_retry(self):
        class Response:
            status_code = 403

        class Session:
            def __init__(self):
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return Response()

        session = Session()
        with patch("app.providers.gallery_dl.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 2 attempts: HTTP 403"):
                GalleryDLProvider._artstation_session_get(session, "https://cdn.test/process.gif")
        self.assertEqual(session.calls, 2)
        sleep.assert_called_once()

    def test_artstation_match_skips_forbidden_process_asset_but_keeps_confidence_gate(self):
        reference = np.full((96, 80, 3), (30, 120, 220), dtype=np.uint8)
        inaccessible = "https://cdn.test/assets/images/images/1/2/3/large/process.gif"
        finished = "https://cdn.test/assets/images/images/4/5/6/large/finished.jpg"
        with patch.object(
            GalleryDLProvider,
            "_artstation_decode_image",
            side_effect=[reference, RuntimeError("HTTP 403"), reference.copy()],
        ):
            selected, score, width, height = GalleryDLProvider._artstation_match_image_asset(
                object(), "https://cdn.test/assets/covers/images/7/8/9/medium/cover.jpg",
                [inaccessible, finished], "https://artist.artstation.com/projects/example",
            )
        self.assertEqual(selected, finished)
        self.assertLessEqual(score, 45)
        self.assertEqual((width, height), (80, 96))

    async def test_artstation_search_keeps_only_cover_matched_original_asset(self):
        instance = provider("artstation")
        cover = "https://cdna.artstation.com/p/assets/images/images/123/456/789/medium/finished.jpg?1"
        common = {
            "id": 44,
            "hash_id": "Are9Re",
            "count": 2,
            "cover_url": cover,
            "userinfo": {"username": "braveking"},
            "date": "2026-01-01T00:00:00+00:00",
        }
        process = {**common, "asset": {
            "id": 100, "image_url": "https://cdna.artstation.com/p/assets/images/images/900/800/700/large/process.jpg",
            "width": 2000, "height": 2000,
        }}
        finished = {**common, "asset": {
            "id": 101, "image_url": "https://cdna.artstation.com/p/assets/images/images/123/456/789/large/finished.jpg?1",
            "width": 4000, "height": 2400,
        }}
        instance._run_gallery_dl = AsyncMock(return_value=[
            ("https://cdn.test/process.jpg", process),
            ("https://cdn.test/finished.jpg", finished),
        ])
        posts, cursor = await instance.search("braveking", limit=10)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].remote_id, "Are9Re:101")
        self.assertEqual(posts[0].image_url, "https://cdn.test/finished.jpg")
        self.assertEqual(posts[0].preview_url, cover)
        self.assertEqual(posts[0].raw_metadata["artstation_selection"]["method"], "cover_asset_path")
        self.assertIsNone(cursor)

    async def test_artstation_scans_past_process_assets_to_fill_requested_works(self):
        instance = provider("artstation")
        first = {
            "hash_id": "first", "count": 2,
            "cover_url": "https://cdn.test/first-final.jpg",
            "userinfo": {"username": "artist"},
            "asset": {"id": 1, "image_url": "https://cdn.test/first-process.jpg"},
        }
        finished = {**first, "asset": {"id": 2, "image_url": "https://cdn.test/first-final.jpg"}}
        second = {
            "hash_id": "second", "count": 1,
            "cover_url": "https://cdn.test/second-final.jpg",
            "userinfo": {"username": "artist"},
            "asset": {"id": 3, "image_url": "https://cdn.test/second-final.jpg"},
        }
        instance._run_gallery_dl = AsyncMock(side_effect=[
            [("https://cdn.test/first-process.jpg", first), ("https://cdn.test/first-final.jpg", finished)],
            [("https://cdn.test/second-final.jpg", second)],
        ])
        posts, cursor = await instance.search("artist", limit=2)
        self.assertEqual(len(posts), 2)
        self.assertEqual([post.remote_id for post in posts], ["first:2", "second:3"])
        self.assertEqual(cursor, "3")
        self.assertEqual(instance._run_gallery_dl.await_args_list[1].args[-2:], (3, 3))

    def test_artstation_ambiguous_multi_asset_project_is_skipped(self):
        messages = [
            ("https://cdn.test/one.jpg", {"hash_id": "project", "count": 2, "asset": {"id": 1, "image_url": "https://cdn.test/one.jpg"}}),
            ("https://cdn.test/two.jpg", {"hash_id": "project", "count": 2, "asset": {"id": 2, "image_url": "https://cdn.test/two.jpg"}}),
        ]
        self.assertEqual(_select_artstation_cover_messages(messages), [])

    def test_artstation_marmoset_cover_uses_high_resolution_still_not_viewer_archive(self):
        image_url = "https://cdn.artstation.com/p/assets/marmosets/001/002/003/large/viewer-cover.jpg"
        common = {
            "hash_id": "model",
            "cover_url": image_url.replace("/large/", "/medium/"),
            "count": 1,
            "asset": {"id": 7, "image_url": image_url, "has_embedded_player": True},
        }
        selected = _select_artstation_cover_messages([
            ("https://cdn.test/model.mview", {**common, "extension": "mview"}),
            ("https://cdn.test/viewer-cover.jpg", {**common, "extension": "jpg"}),
        ])
        self.assertEqual(selected[0][0], "https://cdn.test/viewer-cover.jpg")

    def test_artstation_external_video_uses_attached_project_still(self):
        image_url = "https://cdn.artstation.com/p/assets/images/images/001/002/003/8k/film-cover.jpg"
        common = {
            "hash_id": "external-film",
            "cover_url": image_url.replace("/8k/", "/medium/"),
            "count": 1,
            "asset": {
                "id": 8,
                "image_url": image_url,
                "has_embedded_player": True,
                "player_embedded": '<iframe src="https://player.vimeo.com/video/123"></iframe>',
            },
        }
        selected = _select_artstation_cover_messages([
            ("https://player.vimeo.com/video/123", {**common, "extension": "mp4"}),
            (image_url, {**common, "extension": "jpg"}),
        ])
        self.assertEqual(selected[0][0], image_url)

    def test_artstation_config_filters_before_amount_range(self):
        settings = provider("artstation")._config()["extractor"]["artstation"]
        self.assertEqual(settings["image-filter"], ARTSTATION_COVER_FILTER)
        self.assertEqual(settings["retries"], 0)
        self.assertTrue(settings["videos"])
        self.assertTrue(settings["previews"])

    async def test_search_uses_offset_range_and_post_filters_dates(self):
        instance = provider("twitter")
        instance._run_gallery_dl = AsyncMock(return_value=[("https://pbs.test/a.jpg", {
            "id": "88", "num": 1, "extension": "jpg", "date": "2026-02-03T00:00:00+00:00",
            "author": {"name": "artist_handle", "nick": "Artist"}, "hashtags": ["art"],
        })])
        with patch.object(instance, "_target_url", return_value="https://x.com/artist/media"):
            posts, cursor = await instance.search("artist", cursor="20", limit=10, date_from="2026-01-01")
        instance._run_gallery_dl.assert_awaited_once_with("https://x.com/artist/media", 21, 30)
        self.assertIsNone(cursor)
        self.assertEqual(posts[0].remote_url, "https://x.com/artist_handle/status/88")

    def test_secret_redaction_is_recursive(self):
        self.assertEqual(_public_metadata({"ok": 1, "nested": {"cookie": "x", "name": "safe"}}), {"ok": 1, "nested": {"name": "safe"}})

    def test_twitter_cookie_file_is_validated_stored_and_selected(self):
        content = "\n".join((
            "# Netscape HTTP Cookie File",
            ".x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tsecret-one",
            ".x.com\tTRUE\t/\tTRUE\t2147483647\tct0\tsecret-two",
            "",
        ))
        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "twitter.cookies.txt"
            with patch("app.providers.gallery_dl.TWITTER_COOKIE_PATH", cookie_path), \
                 patch("app.providers.gallery_dl.read_provider_settings", return_value={}), \
                 patch("app.providers.gallery_dl.update_provider_settings") as update:
                result = save_twitter_cookie_file(content)
            self.assertTrue(cookie_path.is_file())
            self.assertEqual(result["auth_mode"], "cookies_file")
            self.assertEqual(result["cookie_count"], 2)
            self.assertNotIn("secret-one", str(result))
            update.assert_called_once_with("twitter", {
                "auth_mode": "cookies_file", "browser": "", "profile": "",
            })

            instance = provider("twitter")
            instance.settings.update({
                "twitter_auth_mode": "cookies_file",
                "twitter_cookie_file": str(cookie_path),
                "twitter_browser": "edge",
            })
            self.assertEqual(instance._config()["extractor"]["twitter"]["cookies"], str(cookie_path))

    def test_twitter_cookie_file_requires_signed_in_x_cookies(self):
        content = ".example.com\tTRUE\t/\tTRUE\t2147483647\tsession\tvalue\n"
        with self.assertRaisesRegex(ValueError, "auth_token"):
            save_twitter_cookie_file(content)


if __name__ == "__main__":
    unittest.main()
