import tempfile
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, features

from app.models import RemotePost
from app.services.media import StaticPreviewUnavailable, _extract_artstation_cover_frame, _extract_rar_frame, archive_decoder_status, download_importable_image, download_importable_images, ensure_image_meets_folder_quality, video_decoder_status


class FixtureProvider:
    def __init__(self, sources: dict[str, Path]):
        self.sources = sources
        self.requested_urls = []

    async def download_image(self, post, dest_path, progress=None):
        self.requested_urls.append(post.image_url)
        source = self.sources.get(post.image_url)
        if source is None:
            raise OSError(f"No fixture for {post.image_url}")
        Path(dest_path).write_bytes(source.read_bytes())
        if progress:
            size = source.stat().st_size
            progress({"stage": "complete", "remote_id": post.remote_id, "bytes_downloaded": size, "bytes_total": size, "percent": 100, "speed_bps": size, "eta_seconds": 0})


def remote_post(preview_url: str | None) -> RemotePost:
    return RemotePost(
        provider="fixture",
        remote_id="video-1",
        remote_url="https://example.test/posts/1",
        image_url="https://example.test/original.webm",
        preview_url=preview_url,
        width=1920,
        height=1080,
        format="webm",
        md5="original-video-md5",
        tags={"general": ["animated"]},
        created_at="2026-09-11T00:00:00Z",
        raw_metadata={"id": 1},
    )


class MediaImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_original_video_yields_three_distinct_full_size_frames(self):
        status = video_decoder_status()
        if not status["available"]:
            self.skipTest("video decoder unavailable")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            colors = ["red", "green", "blue", "yellow", "cyan", "magenta"]
            for index, color in enumerate(colors):
                Image.new("RGB", (640, 512), color).save(root / f"frame_{index:02d}.png")
            original = root / "sequence.mkv"
            subprocess.run([
                status["executable"], "-hide_banner", "-loglevel", "error", "-y",
                "-framerate", "1", "-i", str(root / "frame_%02d.png"),
                "-threads", "1", "-c:v", "ffv1", str(original),
            ], check=True, capture_output=True)
            post = remote_post(None).model_copy(update={"format": "mkv"})
            provider = FixtureProvider({post.image_url: original})
            items = await download_importable_images(provider, post)
            try:
                self.assertEqual(provider.requested_urls, [post.image_url])
                self.assertEqual(len(items), 3)
                self.assertEqual([item[1].remote_id for item in items], [
                    "video-1:frame:1", "video-1:frame:2", "video-1:frame:3",
                ])
                self.assertTrue(all(item[1].width == 640 and item[1].height == 512 for item in items))
                pixels = []
                for path, _, _ in items:
                    with Image.open(path) as frame:
                        pixels.append(frame.convert("RGB").getpixel((320, 256)))
                self.assertEqual(len(set(pixels)), 3)
            finally:
                for path, _, _ in items:
                    Path(path).unlink(missing_ok=True)

    async def test_animated_gif_yields_three_frames_from_one_download(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.gif"
            frames = [Image.new("RGB", (640, 512), color) for color in ("red", "green", "blue", "yellow", "cyan")]
            frames[0].save(original, save_all=True, append_images=frames[1:], duration=100, loop=0)
            for frame in frames:
                frame.close()
            post = remote_post(None).model_copy(update={"image_url": "https://example.test/original.gif", "format": "gif"})
            provider = FixtureProvider({post.image_url: original})
            items = await download_importable_images(provider, post)
            try:
                self.assertEqual(len(items), 3)
                self.assertEqual(provider.requested_urls, [post.image_url])
            finally:
                for path, _, _ in items:
                    Path(path).unlink(missing_ok=True)

    async def test_zip_frame_sequence_yields_three_spaced_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "sequence.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for index, color in enumerate(("red", "green", "blue", "yellow", "cyan")):
                    frame = root / f"frame_{index:02d}.png"
                    Image.new("RGB", (640, 512), color).save(frame)
                    output.write(frame, f"originals/{frame.name}")
            post = remote_post(None).model_copy(update={"image_url": "https://example.test/sequence.zip", "format": "zip"})
            provider = FixtureProvider({post.image_url: archive})
            items = await download_importable_images(provider, post)
            try:
                self.assertEqual(len(items), 3)
                self.assertEqual(provider.requested_urls, [post.image_url])
                self.assertEqual([item[1].remote_id for item in items], [
                    "video-1:frame:1", "video-1:frame:2", "video-1:frame:3",
                ])
                self.assertEqual([item[1].raw_metadata["_artist_collection_import"]["archive_member"] for item in items], [
                    "originals/frame_01.png", "originals/frame_02.png", "originals/frame_03.png",
                ])
            finally:
                for path, _, _ in items:
                    Path(path).unlink(missing_ok=True)

    async def test_zip_contained_video_yields_three_frames(self):
        status = video_decoder_status()
        if not status["available"]:
            self.skipTest("video decoder unavailable")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, color in enumerate(("red", "green", "blue", "yellow", "cyan", "magenta")):
                Image.new("RGB", (640, 512), color).save(root / f"frame_{index:02d}.png")
            video = root / "original.mkv"
            subprocess.run([
                status["executable"], "-hide_banner", "-loglevel", "error", "-y",
                "-framerate", "1", "-i", str(root / "frame_%02d.png"),
                "-threads", "1", "-c:v", "ffv1", str(video),
            ], check=True, capture_output=True)
            archive = root / "video.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.write(video, "originals/original.mkv")
            post = remote_post(None).model_copy(update={"image_url": "https://example.test/video.zip", "format": "zip"})
            provider = FixtureProvider({post.image_url: archive})
            items = await download_importable_images(provider, post)
            try:
                self.assertEqual(len(items), 3)
                self.assertEqual(provider.requested_urls, [post.image_url])
                self.assertTrue(all(item[1].raw_metadata["_artist_collection_import"]["archive_member"] == "originals/original.mkv" for item in items))
            finally:
                for path, _, _ in items:
                    Path(path).unlink(missing_ok=True)

    async def test_video_without_decoder_skips_provider_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            preview = Path(folder) / "preview.png"
            Image.new("RGBA", (96, 64), (10, 20, 30, 128)).save(preview)
            provider = FixtureProvider({"https://example.test/preview.png": preview})
            with patch("app.services.media.video_decoder_status", return_value={"available": False}):
                with self.assertRaisesRegex(StaticPreviewUnavailable, "intentionally skipped"):
                    await download_importable_image(
                        provider, remote_post("https://example.test/preview.png")
                    )
            self.assertEqual(provider.requested_urls, [])

    async def test_video_without_preview_is_skipped_cleanly(self):
        provider = FixtureProvider({})
        with patch("app.services.media.video_decoder_status", return_value={"available": False}):
            with self.assertRaises(StaticPreviewUnavailable):
                await download_importable_image(provider, remote_post(None))
        self.assertEqual(provider.requested_urls, [])

    async def test_broken_static_original_does_not_import_provider_thumbnail(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "broken.png"
            original.write_bytes(b"not a png")
            preview = Path(folder) / "preview.png"
            Image.new("RGB", (96, 64), "red").save(preview)
            post = remote_post("https://example.test/preview.png").model_copy(update={
                "image_url": "https://example.test/original.png",
                "format": "png",
            })
            provider = FixtureProvider({post.image_url: original, post.preview_url: preview})
            with self.assertRaisesRegex(StaticPreviewUnavailable, "intentionally skipped"):
                await download_importable_image(provider, post)
            self.assertEqual(provider.requested_urls, [post.image_url])

    async def test_video_prefers_original_resolution_frame_over_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.webm"
            original.write_bytes(b"video fixture")
            extracted = Path(folder) / "frame.png"
            Image.new("RGB", (640, 360), "green").save(extracted)
            post = remote_post("https://example.test/preview.png")
            provider = FixtureProvider({post.image_url: original})
            with (
                patch("app.services.media.video_decoder_status", return_value={"available": True, "executable": "ffmpeg"}),
                patch("app.services.media._extract_video_frame", return_value=(str(extracted), "png", 640, 360)),
            ):
                path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(path, str(extracted))
                self.assertEqual(derived, "original_frame")
                self.assertEqual((converted_post.width, converted_post.height), (640, 360))
                self.assertEqual(provider.requested_urls, [post.image_url])
                self.assertEqual(
                    converted_post.raw_metadata["_artist_collection_import"]["frame_source_url"],
                    post.image_url,
                )
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_artstation_video_uses_cover_only_to_select_original_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.mp4"
            original.write_bytes(b"video fixture")
            cover = Path(folder) / "cover.jpg"
            Image.new("RGB", (320, 180), "blue").save(cover)
            extracted = Path(folder) / "matched-frame.png"
            Image.new("RGB", (1920, 1080), "blue").save(extracted)
            post = remote_post("https://example.test/cover.jpg").model_copy(update={
                "provider": "artstation",
                "image_url": "https://example.test/original.mp4",
                "format": "mp4",
            })
            provider = FixtureProvider({post.image_url: original, post.preview_url: cover})
            with (
                patch("app.services.media.video_decoder_status", return_value={"available": True, "executable": "ffmpeg"}),
                patch(
                    "app.services.media._extract_artstation_cover_frame",
                    return_value=(str(extracted), "png", 1920, 1080),
                ) as match_cover,
            ):
                path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(path, str(extracted))
                self.assertEqual(derived, "original_frame")
                self.assertEqual(provider.requested_urls, [post.image_url, post.preview_url])
                match_cover.assert_called_once()
                marker = converted_post.raw_metadata["_artist_collection_import"]
                self.assertEqual(marker["frame_selection"], "artstation_cover_match")
                self.assertEqual(marker["frame_reference_url"], post.preview_url)
                self.assertFalse(marker["derived_from_preview"])
            finally:
                Path(path).unlink(missing_ok=True)

    def test_artstation_cover_matcher_keeps_selected_frame_at_video_resolution(self):
        status = video_decoder_status()
        if not status["available"]:
            self.skipTest("Video decoder is unavailable")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index, color in enumerate(("red", "green", "blue")):
                Image.new("RGB", (320, 180), color).save(root / f"frame_{index:02}.png")
            video = root / "sequence.mkv"
            generated = subprocess.run(
                [
                    status["executable"], "-hide_banner", "-loglevel", "error", "-y",
                    "-framerate", "1", "-i", str(root / "frame_%02d.png"),
                    "-threads", "1", "-c:v", "ffv1", str(video),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if generated.returncode != 0:
                self.skipTest(f"Bundled FFmpeg could not create cover-match fixture: {generated.stderr}")
            reference = root / "cover.jpg"
            Image.new("RGB", (160, 90), "green").save(reference, quality=95)
            path, image_format, width, height = _extract_artstation_cover_frame(str(video), str(reference))
            try:
                self.assertEqual(image_format, "png")
                self.assertEqual((width, height), (320, 180))
                with Image.open(path) as frame:
                    red, green, blue = frame.convert("RGB").getpixel((160, 90))
                self.assertGreater(green, red)
                self.assertGreater(green, blue)
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_bundled_ffmpeg_extracts_a_full_resolution_png(self):
        status = video_decoder_status()
        if not status["available"]:
            self.skipTest("Video decoder is unavailable")
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.mkv"
            generated = subprocess.run(
                [
                    status["executable"], "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=0.5",
                    "-threads", "1", "-pix_fmt", "yuv420p", "-c:v", "ffv1", str(original),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if generated.returncode != 0:
                self.skipTest(f"Bundled FFmpeg could not create the test fixture: {generated.stderr}")
            post = remote_post(None).model_copy(update={
                "image_url": "https://example.test/original.mkv",
                "format": "mkv",
                "width": 160,
                "height": 90,
            })
            provider = FixtureProvider({post.image_url: original})
            updates = []
            path, converted_post, derived = await download_importable_image(
                provider, post, progress=updates.append
            )
            try:
                self.assertEqual(derived, "original_frame")
                self.assertEqual(converted_post.format, "png")
                self.assertEqual((converted_post.width, converted_post.height), (160, 90))
                self.assertEqual(provider.requested_urls, [post.image_url])
                with Image.open(path) as converted:
                    self.assertEqual(converted.format, "PNG")
                    self.assertEqual(converted.size, (160, 90))
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_animated_gif_uses_lossless_original_resolution_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.gif"
            frames = [Image.new("RGB", (320, 180), color) for color in ("red", "blue", "green")]
            frames[0].save(original, save_all=True, append_images=frames[1:], duration=50, loop=0)
            for frame in frames:
                frame.close()
            post = remote_post(None).model_copy(update={
                "image_url": "https://example.test/original.gif",
                "format": "gif",
                "width": 320,
                "height": 180,
            })
            provider = FixtureProvider({post.image_url: original})
            updates = []
            path, converted_post, derived = await download_importable_image(
                provider, post, progress=updates.append
            )
            try:
                self.assertEqual(derived, "original_frame")
                self.assertEqual(converted_post.format, "png")
                self.assertEqual((converted_post.width, converted_post.height), (320, 180))
                self.assertEqual(provider.requested_urls, [post.image_url])
                with Image.open(path) as converted:
                    self.assertEqual(converted.format, "PNG")
                    self.assertEqual(converted.size, (320, 180))
                    self.assertEqual(converted.getpixel((0, 0)), (0, 0, 255))
                self.assertIn("complete", [update["stage"] for update in updates])
                self.assertEqual(updates[-1]["stage"], "processing")
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_apng_is_detected_without_provider_specific_format_logic(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.png"
            frames = [Image.new("RGBA", (80, 48), color) for color in ("red", "blue", "green")]
            frames[0].save(original, save_all=True, append_images=frames[1:], duration=50, loop=0)
            for frame in frames:
                frame.close()
            post = remote_post(None).model_copy(update={
                "image_url": "https://example.test/original.png",
                "format": "png",
                "width": 80,
                "height": 48,
            })
            provider = FixtureProvider({post.image_url: original})
            path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(derived, "original_frame")
                self.assertEqual(converted_post.format, "png")
                self.assertEqual((converted_post.width, converted_post.height), (80, 48))
            finally:
                Path(path).unlink(missing_ok=True)

    @unittest.skipUnless(features.check("webp"), "Pillow lacks WebP support")
    async def test_animated_webp_uses_original_resolution_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / "original.webp"
            frames = [Image.new("RGB", (96, 54), color) for color in ("red", "blue", "green")]
            frames[0].save(original, "WEBP", save_all=True, append_images=frames[1:], duration=50, loop=0, lossless=True)
            for frame in frames:
                frame.close()
            post = remote_post(None).model_copy(update={
                "image_url": "https://example.test/original.webp",
                "format": "webp",
                "width": 96,
                "height": 54,
            })
            provider = FixtureProvider({post.image_url: original})
            path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(derived, "original_frame")
                self.assertEqual(converted_post.format, "png")
                self.assertEqual((converted_post.width, converted_post.height), (96, 54))
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_same_video_url_is_never_used_as_a_static_preview(self):
        post = remote_post("https://example.test/original.webm")
        provider = FixtureProvider({})
        with patch("app.services.media.video_decoder_status", return_value={"available": False}):
            with self.assertRaises(StaticPreviewUnavailable):
                await download_importable_image(provider, post)
        self.assertEqual(provider.requested_urls, [])

    async def test_zip_uses_naturally_ordered_middle_original_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            source_folder = Path(folder)
            frames = []
            for name, color in (("frame_001.png", "red"), ("frame_002.png", "blue"), ("frame_010.png", "green")):
                frame = source_folder / name
                Image.new("RGB", (1280, 720), color).save(frame)
                frames.append(frame)
            archive = source_folder / "animation.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("../unsafe.png", b"not an image")
                for frame in frames:
                    output.write(frame, f"frames/{frame.name}")
            post = remote_post("https://example.test/tiny-preview.jpg").model_copy(update={
                "image_url": "https://example.test/animation.zip",
                "format": "zip",
                "width": 127,
                "height": 180,
            })
            provider = FixtureProvider({post.image_url: archive})

            path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(derived, "archive_frame")
                self.assertEqual((converted_post.width, converted_post.height), (1280, 720))
                self.assertEqual(provider.requested_urls, [post.image_url])
                marker = converted_post.raw_metadata["_artist_collection_import"]
                self.assertEqual(marker["archive_member"], "frames/frame_002.png")
                self.assertEqual(marker["archive_candidate_count"], 3)
                self.assertFalse(marker["derived_from_preview"])
                with Image.open(path) as extracted:
                    self.assertEqual(extracted.size, (1280, 720))
                    self.assertEqual(extracted.getpixel((0, 0)), (0, 0, 255))
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_zip_prefers_dominant_high_resolution_sequence_over_thumbnails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "mixed.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for index in range(10):
                    thumb = root / f"thumb_{index:03}.png"
                    Image.new("RGB", (100, 100), "red").save(thumb)
                    output.write(thumb, f"thumbs/{thumb.name}")
                for index, color in enumerate(("green", "blue", "yellow"), 1):
                    frame = root / f"frame_{index:03}.png"
                    Image.new("RGB", (400, 300), color).save(frame)
                    output.write(frame, f"originals/{frame.name}")
            post = remote_post(None).model_copy(update={
                "image_url": "https://example.test/mixed.zip",
                "format": "zip",
            })
            provider = FixtureProvider({post.image_url: archive})
            path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(derived, "archive_frame")
                self.assertEqual((converted_post.width, converted_post.height), (400, 300))
                marker = converted_post.raw_metadata["_artist_collection_import"]
                self.assertEqual(marker["archive_member"], "originals/frame_002.png")
            finally:
                Path(path).unlink(missing_ok=True)

    async def test_broken_zip_skips_instead_of_importing_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "broken.zip"
            archive.write_bytes(b"not a zip")
            preview = Path(folder) / "preview.png"
            Image.new("RGB", (100, 100), "red").save(preview)
            post = remote_post("https://example.test/preview.png").model_copy(update={
                "image_url": "https://example.test/broken.zip",
                "format": "zip",
            })
            provider = FixtureProvider({post.image_url: archive, post.preview_url: preview})
            with self.assertRaisesRegex(StaticPreviewUnavailable, "original-resolution frame"):
                await download_importable_image(provider, post)
            self.assertEqual(provider.requested_urls, [post.image_url])

    async def test_zip_can_decode_a_contained_video_when_no_still_frames_exist(self):
        status = video_decoder_status()
        if not status["available"]:
            self.skipTest("Video decoder is unavailable")
        with tempfile.TemporaryDirectory() as folder:
            contained_video = Path(folder) / "clip.mkv"
            generated = subprocess.run(
                [
                    status["executable"], "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=orange:s=320x180:d=0.5",
                    "-threads", "1", "-pix_fmt", "yuv420p", "-c:v", "ffv1", str(contained_video),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if generated.returncode != 0:
                self.skipTest(f"Bundled FFmpeg could not create the fixture: {generated.stderr}")
            archive = Path(folder) / "video-only.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.write(contained_video, "animation/clip.mkv")
            post = remote_post("https://example.test/preview.jpg").model_copy(update={
                "image_url": "https://example.test/video-only.zip",
                "format": "zip",
            })
            provider = FixtureProvider({post.image_url: archive})
            path, converted_post, derived = await download_importable_image(provider, post)
            try:
                self.assertEqual(derived, "archive_frame")
                self.assertEqual((converted_post.width, converted_post.height), (320, 180))
                self.assertEqual(
                    converted_post.raw_metadata["_artist_collection_import"]["archive_member"],
                    "animation/clip.mkv",
                )
                with Image.open(path) as decoded:
                    self.assertEqual(decoded.format, "PNG")
                    self.assertEqual(decoded.size, (320, 180))
            finally:
                Path(path).unlink(missing_ok=True)

    def test_archive_decoder_reports_native_zip_and_rar_capability(self):
        status = archive_decoder_status()
        self.assertTrue(status["zip"]["available"])
        self.assertEqual(status["zip"]["backend"], "Python zipfile")
        self.assertIn("available", status["rar"])

    def test_folder_quality_floor_rejects_small_original_archive_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            frame = Path(folder) / "frame.jpg"
            Image.new("RGB", (396, 560), "blue").save(frame)
            with self.assertRaisesRegex(StaticPreviewUnavailable, "396x560.*512x512"):
                ensure_image_meets_folder_quality(
                    str(frame),
                    {"min_width": 512, "min_height": 512, "min_aspect_ratio": 0.33, "max_aspect_ratio": 3.0},
                )

    def test_bsdtar_streams_one_archive_member_without_unpacking_tree(self):
        status = archive_decoder_status()
        if not status["rar"]["available"]:
            self.skipTest("bsdtar is unavailable")
        # A ZIP container is sufficient to exercise the same bsdtar list and
        # bounded stdout extraction commands used for RAR/CBR originals.
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "decoder-fixture.zip"
            frame = Path(folder) / "frame_0001.png"
            Image.new("RGB", (64, 36), "purple").save(frame)
            with zipfile.ZipFile(archive, "w") as output:
                output.write(frame, "sequence/frame_0001.png")
            path, member, count = _extract_rar_frame(str(archive))
            try:
                self.assertEqual(member, "sequence/frame_0001.png")
                self.assertEqual(count, 1)
                with Image.open(path) as decoded:
                    self.assertEqual(decoded.size, (64, 36))
            finally:
                Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
