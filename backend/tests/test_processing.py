import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
from PIL import Image, features

from app.models import RemotePost
from app.services.media import MAX_TRAINING_DIMENSION, prepare_training_image, ensure_image_meets_folder_quality, StaticPreviewUnavailable


def post_for(path: Path, image_format: str, width: int, height: int) -> RemotePost:
    return RemotePost(
        provider="fixture",
        remote_id=path.stem,
        remote_url="https://example.test/post",
        image_url="https://example.test/original",
        width=width,
        height=height,
        format=image_format,
        md5="provider-original-md5",
        tags={"artist": ["fixture_artist"]},
        created_at="2026-09-12T00:00:00Z",
        raw_metadata={"provider_field": "preserved"},
    )


class TrainingImageProcessingTests(unittest.TestCase):
    def test_quality_header_check_honors_orientation_without_pixel_decode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "oriented.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (600, 900), "navy").save(path, exif=exif)
            from PIL import JpegImagePlugin
            with patch.object(JpegImagePlugin.JpegImageFile, "load", side_effect=AssertionError("unnecessary full decode")):
                ensure_image_meets_folder_quality(str(path), {"min_width": 800})
                with self.assertRaises(StaticPreviewUnavailable):
                    ensure_image_meets_folder_quality(str(path), {"min_height": 800})

    @unittest.skipUnless(features.check("webp"), "Pillow lacks WebP support")
    def test_png_is_area_downscaled_then_encoded_as_lossless_webp(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.png"
            Image.new("RGBA", (2400, 1200), (11, 22, 33, 144)).save(path)
            with patch("app.services.media.cv2.resize", wraps=cv2.resize) as resize:
                processed = prepare_training_image(str(path), post_for(path, "png", 2400, 1200))

            self.assertEqual(processed.format, "webp")
            self.assertEqual((processed.width, processed.height), (MAX_TRAINING_DIMENSION, 1000))
            self.assertIsNone(processed.md5)
            resize.assert_called_once()
            self.assertEqual(resize.call_args.kwargs["interpolation"], cv2.INTER_AREA)
            with Image.open(path) as image:
                image.load()
                self.assertEqual(image.format, "WEBP")
                self.assertEqual(image.size, (2000, 1000))
                self.assertEqual(image.getpixel((100, 100)), (11, 22, 33, 144))

            marker = processed.raw_metadata["_artist_folder_processing"]
            self.assertTrue(marker["downscaled"])
            self.assertTrue(marker["lossless_webp"])
            self.assertEqual(marker["resize_interpolation"], "cv2.INTER_AREA")
            self.assertEqual(processed.raw_metadata["provider_field"], "preserved")

    def test_jpeg_stays_jpeg_without_upscaling(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "small.jpg"
            Image.new("RGB", (640, 480), (80, 90, 100)).save(path, "JPEG", quality=90)
            with patch("app.services.media.cv2.resize", wraps=cv2.resize) as resize:
                processed = prepare_training_image(str(path), post_for(path, "jpg", 640, 480))

            self.assertEqual(processed.format, "jpg")
            self.assertEqual((processed.width, processed.height), (640, 480))
            resize.assert_not_called()
            with Image.open(path) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (640, 480))
            marker = processed.raw_metadata["_artist_folder_processing"]
            self.assertFalse(marker["downscaled"])
            self.assertEqual(marker["jpeg_quality"], 100)


if __name__ == "__main__":
    unittest.main()


class CustomProcessingTests(unittest.TestCase):
    def test_disabled_preserves_original_bytes_and_orientation_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'original.png'
            Image.new('RGBA',(2400,900),(10,20,30,100)).save(path)
            before=path.read_bytes()
            processed=prepare_training_image(str(path),post_for(path,'png',2400,900),{'enabled':False})
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual((processed.width,processed.height,processed.format),(2400,900,'png'))
            self.assertTrue(processed.raw_metadata['_artist_folder_processing']['byte_preserved'])
    def test_custom_resize_output_and_no_upscale(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'input.png';Image.new('RGB',(800,600),'navy').save(path)
            processed=prepare_training_image(str(path),post_for(path,'png',800,600),{'max_dimension':400,'output_format':'png','resize_filter':'cubic'})
            self.assertEqual((processed.width,processed.height,processed.format),(400,300,'png'))
            self.assertEqual(processed.raw_metadata['_artist_folder_processing']['resize_interpolation'],'cv2.INTER_CUBIC')
            processed=prepare_training_image(str(path),processed,{'max_dimension':2000,'output_format':'webp','webp_lossless':False,'webp_quality':42,'webp_method':2})
            self.assertEqual((processed.width,processed.height),(400,300))
            self.assertFalse(processed.raw_metadata['_artist_folder_processing']['lossless_webp'])
    def test_no_size_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'large.png';Image.new('RGB',(2300,700),'navy').save(path)
            processed=prepare_training_image(str(path),post_for(path,'png',2300,700),{'max_dimension':None,'output_format':'jpg','jpeg_quality':82})
            self.assertEqual(processed.width,2300)
            self.assertEqual(processed.raw_metadata['_artist_folder_processing']['jpeg_quality'],82)


import test_qa_export
class ProcessingPolicyQATests(unittest.TestCase):
    setUp = test_qa_export.DatasetQAExportTests.setUp
    tearDown = test_qa_export.DatasetQAExportTests.tearDown
    def test_preserved_png_uses_its_import_policy_after_settings_change(self):
        import app.db as db
        from app.services.settings import set_processing_settings
        from app.services.images import ImageService
        from app.services.qa import DatasetQAService
        path=self.root/'large.png';Image.new('RGB',(2300,800),'blue').save(path)
        before=path.read_bytes();set_processing_settings({'enabled':False})
        image_id,_=ImageService(self.root).ingest_image(str(path),post_for(path,'png',2300,800),1)
        set_processing_settings({'enabled':True,'max_dimension':512})
        conn=db.get_connection();row=conn.execute('SELECT path,processing_policy FROM image WHERE id=?',(image_id,)).fetchone();conn.close()
        self.assertEqual((self.root/'images'/row[0]).read_bytes(),before)
        issues=DatasetQAService(self.root).validate_collection(1,[image_id])['issues']
        self.assertFalse([issue for issue in issues if issue['code'] in {'oversized_image','noncanonical_training_format'}])
