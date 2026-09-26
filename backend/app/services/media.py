"""Decode training stills from original images, animations, videos, and archives."""

from __future__ import annotations

import asyncio
import tempfile
import subprocess
import re
import shutil
import stat
import threading
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from app.models import RemotePost


SUPPORTED_IMAGE_FORMATS = {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "avif"}
# Pillow can expose animation in GIF, WebP, PNG/APNG, and AVIF without the
# provider needing to identify those variants specially. GIF is listed here
# because it is never a trainer-facing still format; the other formats are
# detected after opening the downloaded original.
PIL_IMAGE_FORMATS = SUPPORTED_IMAGE_FORMATS | {"gif"}
VIDEO_FORMATS = {
    "webm", "mp4", "mkv", "mov", "avi", "flv", "m4v", "ogv", "ogg",
    "mpg", "mpeg", "3gp", "ts", "m2ts",
}
ZIP_FORMATS = {"zip", "cbz"}
RAR_FORMATS = {"rar", "cbr"}
ARCHIVE_FORMATS = ZIP_FORMATS | RAR_FORMATS
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FRAME_SIZE = 512 * 1024 * 1024
MIN_TRAINING_DIMENSION = 512
MAX_TRAINING_DIMENSION = 2000
WEBP_METHOD = 4
JPEG_QUALITY = 100


class StaticPreviewUnavailable(ValueError):
    pass


DownloadProgress = Callable[[dict], None]
VIDEO_FRAME_COUNT = 3


async def run_blocking_safely(function, *args, **kwargs):
    """Run CPU/file work in a thread and drain it before honoring cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A Python thread cannot be force-stopped. Wait for it before callers
        # remove temporary inputs, then propagate cancellation.
        try:
            await task
        finally:
            raise


def _report_processing(progress: DownloadProgress | None, post: RemotePost, stage: str = "processing") -> None:
    if progress:
        progress({"stage": stage, "remote_id": post.remote_id, "format": post.format})


def normalized_format(value: str | None) -> str:
    result = (value or "").lower().lstrip(".")
    return "jpg" if result in {"jpg", "jpeg", "jpe"} else result


def _numbered_frame_post(post: RemotePost, output_format: str, width: int, height: int,
                         source: str, number: int, total: int, position: float) -> RemotePost:
    derived = _derived_post(post, output_format, width, height, source, post.image_url)
    metadata = dict(derived.raw_metadata)
    marker = dict(metadata["_artist_collection_import"])
    marker.update({"source_remote_id": post.remote_id, "frame_number": number,
                   "frame_count": total, "frame_position": round(position, 5)})
    metadata["_artist_collection_import"] = marker
    return derived.model_copy(update={
        "remote_id": f"{post.remote_id}:frame:{number}", "raw_metadata": metadata,
    })


async def download_importable_images(
    provider, post: RemotePost, progress: DownloadProgress | None = None,
) -> list[tuple[str, RemotePost, str | None]]:
    """Download original media once and return up to three distinct stills for motion media."""
    original_format = normalized_format(post.format)
    if original_format in ARCHIVE_FORMATS:
        return await download_archive_frames(provider, post, progress)
    if original_format not in VIDEO_FORMATS | PIL_IMAGE_FORMATS:
        return [await download_importable_image(provider, post, progress)]
    if original_format in VIDEO_FORMATS and not video_decoder_status()["available"]:
        raise StaticPreviewUnavailable("A video decoder is not installed; the provider preview was intentionally skipped")

    raw_path = _new_temp_path(f".{original_format}")
    reference_path: str | None = None
    frames: list[tuple[str, str, int, int, float]] = []
    keep_raw = False
    downloaded = False
    try:
        await provider.download_image(post, raw_path, progress=progress)
        downloaded = True
        _report_processing(progress, post, "extracting")
        if original_format in VIDEO_FORMATS:
            if post.provider == "artstation" and post.preview_url and post.preview_url != post.image_url:
                reference_format = normalized_format(Path(urlparse(post.preview_url).path).suffix) or "jpg"
                reference_path = _new_temp_path(f".{reference_format}")
                reference_post = post.model_copy(update={"image_url": post.preview_url, "format": reference_format, "md5": None})
                try:
                    await provider.download_image(reference_post, reference_path)
                except Exception:
                    Path(reference_path).unlink(missing_ok=True)
                    reference_path = None
            frames = await run_blocking_safely(_extract_video_frames, raw_path, reference_path)
        else:
            with Image.open(raw_path) as source:
                animated = bool(getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1)
                count = int(getattr(source, "n_frames", 1))
            if not animated:
                # Keep normal stills on the established one-image path without
                # downloading them a second time.
                if original_format != "gif":
                    keep_raw = True
                    return [(raw_path, post, None)]
                path, fmt, width, height = await run_blocking_safely(_convert_first_frame, raw_path, False, True)
                frames = [(path, fmt, width, height, 0.0)]
            else:
                indices = _spaced_frame_indices(count)
                for index in indices:
                    path, fmt, width, height = await run_blocking_safely(_convert_frame_at, raw_path, index)
                    frames.append((path, fmt, width, height, index / max(1, count - 1)))
        if not frames:
            raise StaticPreviewUnavailable("Original media had no decodable frames")
        total = len(frames)
        return [
            (path, _numbered_frame_post(post, fmt, width, height, "original_frame", number, total, position), "original_frame")
            for number, (path, fmt, width, height, position) in enumerate(frames, 1)
        ]
    except BaseException as exc:
        for path, *_ in frames:
            Path(path).unlink(missing_ok=True)
        if downloaded and isinstance(exc, (UnidentifiedImageError, OSError, ValueError, subprocess.SubprocessError)):
            raise StaticPreviewUnavailable(
                f"Could not decode original-resolution {original_format.upper()} media; provider preview was intentionally skipped: {exc}"
            ) from exc
        raise
    finally:
        if not keep_raw:
            Path(raw_path).unlink(missing_ok=True)
        if reference_path:
            Path(reference_path).unlink(missing_ok=True)


def _spaced_frame_indices(count: int) -> list[int]:
    if count < 1:
        return []
    return sorted({min(count - 1, round((count - 1) * fraction)) for fraction in (0.2, 0.5, 0.8)})


def _convert_frame_at(raw_path: str, index: int) -> tuple[str, str, int, int]:
    output_path: str | None = None
    try:
        with Image.open(raw_path) as source:
            source.seek(index)
            frame = ImageOps.exif_transpose(source)
            frame.load()
            width, height = frame.size
            if width < 1 or height < 1:
                raise ValueError("Animation frame has invalid dimensions")
            output_path = _new_temp_path(".png")
            has_alpha = "A" in frame.getbands() or (frame.mode == "P" and "transparency" in frame.info)
            converted = frame.convert("RGBA" if has_alpha else "RGB")
            converted.save(output_path, "PNG", compress_level=0)
            converted.close()
            if frame is not source:
                frame.close()
        return output_path, "png", width, height
    except BaseException:
        if output_path:
            Path(output_path).unlink(missing_ok=True)
        raise


async def download_archive_frames(
    provider, post: RemotePost, progress: DownloadProgress | None = None,
) -> list[tuple[str, RemotePost, str]]:
    """Extract three full-size stills from an archive sequence or contained video."""
    original_format = normalized_format(post.format)
    if original_format in RAR_FORMATS and not archive_decoder_status()["rar"]["available"]:
        raise StaticPreviewUnavailable("A RAR decoder is not available; ZIP archives remain supported")
    archive_path = _new_temp_path(f".{original_format}")
    extracted: list[str] = []
    keep: set[str] = set()
    downloaded = False
    try:
        await provider.download_image(post, archive_path, progress=progress)
        downloaded = True
        _report_processing(progress, post, "extracting")
        if Path(archive_path).stat().st_size > MAX_ARCHIVE_SIZE:
            raise ValueError("Archive exceeds the 2 GiB safety limit")
        members = await run_blocking_safely(
            _extract_zip_sequence_frames if original_format in ZIP_FORMATS else _extract_rar_sequence_frames,
            archive_path,
        )
        extracted = [path for path, _, _, _ in members]
        decoded: list[tuple[str, str, int, int, float, str, int]] = []
        for member_path, name, count, position in members:
            if normalized_format(Path(name).suffix) in VIDEO_FORMATS:
                for path, fmt, width, height, frame_position in await run_blocking_safely(
                    _extract_video_frames, member_path
                ):
                    extracted.append(path)
                    decoded.append((path, fmt, width, height, frame_position, name, count))
            else:
                path, fmt, width, height = await run_blocking_safely(
                    _normalize_extracted_archive_frame, member_path
                )
                extracted.append(path)
                decoded.append((path, fmt, width, height, position, name, count))
        decoded = decoded[:VIDEO_FRAME_COUNT]
        total = len(decoded)
        result = []
        for number, (path, fmt, width, height, position, name, count) in enumerate(decoded, 1):
            derived = _numbered_frame_post(post, fmt, width, height, "archive_frame", number, total, position)
            metadata = dict(derived.raw_metadata)
            marker = dict(metadata["_artist_collection_import"])
            marker.update({"archive_member": name, "archive_candidate_count": count,
                           "archive_decoder": "Python zipfile" if original_format in ZIP_FORMATS else "bsdtar"})
            metadata["_artist_collection_import"] = marker
            result.append((path, derived.model_copy(update={"raw_metadata": metadata}), "archive_frame"))
            keep.add(path)
        return result
    except BaseException as exc:
        for path in keep:
            Path(path).unlink(missing_ok=True)
        if downloaded and isinstance(exc, (UnidentifiedImageError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile)):
            raise StaticPreviewUnavailable(
                f"Could not extract original-resolution {original_format.upper()} frames: {exc}"
            ) from exc
        raise
    finally:
        for path in extracted:
            if path not in keep:
                Path(path).unlink(missing_ok=True)
        Path(archive_path).unlink(missing_ok=True)


def ensure_image_meets_folder_quality(raw_path: str, filters: dict | None) -> None:
    """Reject decoded originals that do not meet the folder's quality floor."""
    filters = filters or {}
    with Image.open(raw_path) as source:
        # Dimensions and EXIF orientation suffice here. Decoding/transposing
        # all pixels duplicated the work performed by prepare_training_image.
        # That normalization step still fully decodes and validates the input.
        # Pillow may still decode PNG to find EXIF after IDAT; preserve that
        # behavior rather than ignoring late orientation metadata for speed.
        width, height = source.size
        if source.getexif().get(274) in (5, 6, 7, 8):
            width, height = height, width
    min_width = max(MIN_TRAINING_DIMENSION, int(filters.get("min_width", MIN_TRAINING_DIMENSION) or MIN_TRAINING_DIMENSION))
    min_height = max(MIN_TRAINING_DIMENSION, int(filters.get("min_height", MIN_TRAINING_DIMENSION) or MIN_TRAINING_DIMENSION))
    if width < min_width or height < min_height:
        raise StaticPreviewUnavailable(
            f"Original-quality frame is only {width}x{height}; folder minimum is {min_width}x{min_height}"
        )
    ratio = width / height
    min_ratio = float(filters.get("min_aspect_ratio", 0) or 0)
    max_ratio = float(filters.get("max_aspect_ratio", 0) or 0)
    if min_ratio and ratio < min_ratio:
        raise StaticPreviewUnavailable(
            f"Original-quality frame aspect ratio {ratio:.3f} is below the folder minimum {min_ratio:g}"
        )
    if max_ratio and ratio > max_ratio:
        raise StaticPreviewUnavailable(
            f"Original-quality frame aspect ratio {ratio:.3f} exceeds the folder maximum {max_ratio:g}"
        )


def prepare_training_image(raw_path: str, post: RemotePost, settings=None) -> RemotePost:
    """Normalize one decoded still in place before hashing and storage.

    Pixels are decoded once, optionally downscaled with OpenCV INTER_AREA, and
    then encoded once to the final trainer format. JPEG inputs remain JPEG;
    every other supported still becomes lossless WebP.
    """
    from app.models import ProcessingSettings
    policy = ProcessingSettings.model_validate(settings or {}).model_dump()
    if not policy['enabled']:
        with Image.open(raw_path) as original:
            if getattr(original, 'is_animated', False):
                raise ValueError('Extract animation frames before ingesting an unprocessed still')
            original.load()
            fmt = normalized_format(original.format)
            width, height = original.size
            if original.getexif().get(274) in (5, 6, 7, 8): width, height = height, width
        metadata = dict(post.raw_metadata)
        metadata['_artist_folder_processing'] = dict(policy, version=2, output_format=fmt, output_width=width, output_height=height, source_format=fmt, source_width=width, source_height=height, downscaled=False, byte_preserved=True)
        return post.model_copy(update={'format':fmt, 'width':width, 'height':height, 'raw_metadata':metadata})
    interpolation = {'area':cv2.INTER_AREA, 'lanczos':cv2.INTER_LANCZOS4, 'cubic':cv2.INTER_CUBIC, 'linear':cv2.INTER_LINEAR}[policy['resize_filter']]
    output_path: str | None = None
    try:
        with Image.open(raw_path) as source:
            source.seek(0)
            source_format = normalized_format(source.format or Path(raw_path).suffix)
            source_info = dict(source.info)
            frame = ImageOps.exif_transpose(source)
            frame.load()
            source_width, source_height = frame.size
            if source_width < 1 or source_height < 1:
                raise ValueError("Image has invalid dimensions")

            has_alpha = frame.mode in {"RGBA", "LA"} or (
                frame.mode == "P" and "transparency" in frame.info
            )
            working = frame.convert("RGBA" if has_alpha and source_format != "jpg" else "RGB")
            pixels = np.asarray(working)
            output_width, output_height = source_width, source_height
            downscaled = bool(policy['max_dimension'] and max(source_width, source_height) > policy['max_dimension'])
            if downscaled:
                scale = policy['max_dimension'] / max(source_width, source_height)
                output_width = max(1, round(source_width * scale))
                output_height = max(1, round(source_height * scale))
                pixels = cv2.resize(
                    pixels,
                    (output_width, output_height),
                    interpolation=interpolation,
                )

            output_format = ("jpg" if source_format == "jpg" else "webp") if policy["output_format"] == "auto" else policy["output_format"]
            output_path = _new_temp_path(f".{output_format}")
            encoded = Image.fromarray(pixels)
            save_options = {}
            icc_profile = source_info.get("icc_profile")
            if icc_profile:
                save_options["icc_profile"] = icc_profile
            if output_format == "jpg":
                encoded = encoded.convert("RGB")
                encoded.save(
                    output_path,
                    "JPEG",
                    quality=policy["jpeg_quality"],
                    subsampling=0,
                    optimize=True,
                    **save_options,
                )
            elif output_format == "png":
                encoded.save(output_path, "PNG", **save_options)
            else:
                encoded.save(
                    output_path,
                    "WEBP",
                    lossless=policy["webp_lossless"],
                    quality=policy["webp_quality"],
                    method=policy["webp_method"],
                    exact=True,
                    **save_options,
                )
            encoded.close()
            working.close()
            if frame is not source:
                frame.close()

        # Keep the caller's temporary path stable so its existing cleanup
        # logic remains reliable even though the encoded format may change.
        # App-owned scratch and an uploaded/imported temporary input can live
        # on different volumes. move falls back to copying across volumes.
        shutil.move(output_path, raw_path)
        output_path = None

        metadata = dict(post.raw_metadata)
        existing_marker = metadata.get("_artist_collection_import")
        if isinstance(existing_marker, dict):
            marker = dict(existing_marker)
            marker["converted_format"] = output_format
            marker["converted_width"] = output_width
            marker["converted_height"] = output_height
            metadata["_artist_collection_import"] = marker
        metadata["_artist_folder_processing"] = {
            **policy,
            "version": 2,
            "source_format": source_format,
            "source_width": source_width,
            "source_height": source_height,
            "output_format": output_format,
            "output_width": output_width,
            "output_height": output_height,
            "downscaled": downscaled,
            "max_dimension": policy["max_dimension"],
            "resize_interpolation": "cv2.INTER_" + {"area":"AREA","lanczos":"LANCZOS4","linear":"LINEAR","cubic":"CUBIC"}[policy["resize_filter"]] if downscaled else None,
            "lossless_webp": output_format == "webp" and policy["webp_lossless"],
            "webp_method": policy["webp_method"] if output_format == "webp" else None,
            "jpeg_quality": policy["jpeg_quality"] if output_format == "jpg" else None,
        }
        return post.model_copy(update={
            "format": output_format,
            "width": output_width,
            "height": output_height,
            # Provider MD5 describes the downloaded original, not the newly
            # encoded trainer file. The ingest service hashes final bytes.
            "md5": None,
            "raw_metadata": metadata,
        })
    except Exception:
        if output_path:
            Path(output_path).unlink(missing_ok=True)
        raise


def video_decoder_status() -> dict:
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        return {"available": bool(executable and Path(executable).exists()), "executable": executable, "backend": "imageio-ffmpeg"}
    except Exception as exc:
        return {"available": False, "executable": None, "backend": None, "error": str(exc)}


def archive_decoder_status() -> dict:
    """Report safe archive-frame support available on this machine."""
    executable = shutil.which("tar") or shutil.which("bsdtar")
    rar_available = False
    error = None
    if executable:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            rar_available = result.returncode == 0 and "bsdtar" in (result.stdout + result.stderr).lower()
            if not rar_available:
                error = "The installed tar program is not a libarchive/bsdtar decoder"
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
    else:
        error = "bsdtar was not found"
    return {
        "zip": {"available": True, "backend": "Python zipfile"},
        "rar": {
            "available": rar_available,
            "backend": "bsdtar" if rar_available else None,
            "executable": executable if rar_available else None,
            "error": error,
        },
    }


def media_decoder_status() -> dict:
    status = video_decoder_status()
    status["archives"] = archive_decoder_status()
    return status


async def download_importable_image(
    provider,
    post: RemotePost,
    progress: DownloadProgress | None = None,
) -> tuple[str, RemotePost, str | None]:
    """Return (temporary path, ingest metadata, derived media source)."""
    original_format = normalized_format(post.format)
    if original_format in PIL_IMAGE_FORMATS:
        raw_path = _new_temp_path(f".{original_format}")
        try:
            await provider.download_image(post, raw_path, progress=progress)
            _report_processing(progress, post)
            with Image.open(raw_path) as source:
                source.load()
                animated = bool(getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1)
            if not animated:
                if original_format == "gif":
                    # A provider may label a single-frame GIF as GIF. It still
                    # needs conversion because trainers generally do not
                    # accept GIF files as source images.
                    converted_path, output_format, width, height = await run_blocking_safely(
                        _convert_first_frame, raw_path, representative=False, lossless=True
                    )
                    return _finish_original_frame(post, raw_path, converted_path, output_format, width, height)
                return raw_path, post, None
            converted_path, output_format, width, height = await run_blocking_safely(
                _convert_first_frame, raw_path, representative=True, lossless=True
            )
            return _finish_original_frame(post, raw_path, converted_path, output_format, width, height)
        except asyncio.CancelledError:
            Path(raw_path).unlink(missing_ok=True)
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            Path(raw_path).unlink(missing_ok=True)
            raise StaticPreviewUnavailable(
                f"Could not decode the original-resolution {original_format.upper()} image; provider preview was intentionally skipped: {exc}"
            ) from exc

    if original_format in ARCHIVE_FORMATS:
        try:
            return await download_archive_frame(provider, post, progress)
        except StaticPreviewUnavailable:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, zipfile.BadZipFile) as exc:
            raise StaticPreviewUnavailable(
                f"Could not extract an original-resolution frame from the {original_format.upper()} archive: {exc}"
            ) from exc

    if original_format in VIDEO_FORMATS:
        if not video_decoder_status()["available"]:
            raise StaticPreviewUnavailable(
                "A video decoder is not installed; the low-resolution provider preview was intentionally skipped"
            )
        try:
            return await download_original_frame(provider, post, progress)
        except StaticPreviewUnavailable:
            raise
        except (UnidentifiedImageError, OSError, subprocess.SubprocessError, ValueError) as exc:
            raise StaticPreviewUnavailable(
                f"Could not extract an original-resolution video frame; the low-resolution provider preview was intentionally skipped: {exc}"
            ) from exc

    raise StaticPreviewUnavailable(
        f"{post.format or 'Unknown'} original could not yield a full-quality training still; provider preview was intentionally skipped"
    )


async def download_archive_frame(
    provider,
    post: RemotePost,
    progress: DownloadProgress | None = None,
) -> tuple[str, RemotePost, str]:
    """Download an archive original and return one full-quality middle frame."""
    original_format = normalized_format(post.format)
    if original_format not in ARCHIVE_FORMATS:
        raise StaticPreviewUnavailable(f"{post.format or 'Unknown'} is not a supported archive")
    if original_format in RAR_FORMATS and not archive_decoder_status()["rar"]["available"]:
        raise StaticPreviewUnavailable("A RAR decoder is not available; ZIP archives remain supported")

    archive_path = _new_temp_path(f".{original_format}")
    try:
        await provider.download_image(post, archive_path, progress=progress)
        _report_processing(progress, post, "extracting")
        archive_size = Path(archive_path).stat().st_size
        if archive_size > MAX_ARCHIVE_SIZE:
            raise ValueError("Archive exceeds the 2 GiB safety limit")
        if original_format in ZIP_FORMATS:
            frame_path, member_name, candidate_count = await run_blocking_safely(
                _extract_zip_frame, archive_path
            )
        else:
            frame_path, member_name, candidate_count = await run_blocking_safely(
                _extract_rar_frame, archive_path
            )
    finally:
        Path(archive_path).unlink(missing_ok=True)

    try:
        frame_path, output_format, width, height = await run_blocking_safely(
            _normalize_extracted_archive_frame, frame_path
        )
    except Exception:
        Path(frame_path).unlink(missing_ok=True)
        raise
    derived_post = _derived_post(post, output_format, width, height, "archive_frame", post.image_url)
    metadata = dict(derived_post.raw_metadata)
    marker = dict(metadata["_artist_collection_import"])
    marker.update({
        "archive_member": member_name,
        "archive_candidate_count": candidate_count,
        "archive_decoder": "Python zipfile" if original_format in ZIP_FORMATS else "bsdtar",
    })
    metadata["_artist_collection_import"] = marker
    derived_post = derived_post.model_copy(update={"raw_metadata": metadata})
    return frame_path, derived_post, "archive_frame"


async def download_original_frame(
    provider,
    post: RemotePost,
    progress: DownloadProgress | None = None,
) -> tuple[str, RemotePost, str]:
    original_format = normalized_format(post.format)
    if original_format not in PIL_IMAGE_FORMATS | VIDEO_FORMATS:
        raise StaticPreviewUnavailable(f"{post.format or 'Unknown'} is not extractable media")
    if original_format in VIDEO_FORMATS and not video_decoder_status()["available"]:
        raise StaticPreviewUnavailable("A video decoder is not installed; install project requirements to enable full-resolution frame extraction")

    raw_path = _new_temp_path(f".{original_format}")
    reference_path: str | None = None
    frame_selection = "representative"
    try:
        await provider.download_image(post, raw_path, progress=progress)
        _report_processing(progress, post, "extracting" if original_format in VIDEO_FORMATS else "processing")
        if original_format in PIL_IMAGE_FORMATS:
            converted_path, output_format, width, height = await run_blocking_safely(
                _convert_first_frame, raw_path, representative=True, lossless=True
            )
        else:
            if post.provider == "artstation" and post.preview_url and post.preview_url != post.image_url:
                reference_format = normalized_format(Path(urlparse(post.preview_url).path).suffix) or "jpg"
                reference_path = _new_temp_path(f".{reference_format}")
                reference_post = post.model_copy(update={
                    "image_url": post.preview_url,
                    "format": reference_format,
                    "md5": None,
                })
                try:
                    # This low-resolution cover is used only to locate the
                    # corresponding frame in the original video. It is never
                    # stored or passed to the training normalizer.
                    await provider.download_image(reference_post, reference_path)
                    converted_path, output_format, width, height = await run_blocking_safely(
                        _extract_artstation_cover_frame, raw_path, reference_path
                    )
                    frame_selection = "artstation_cover_match"
                except (OSError, subprocess.SubprocessError, ValueError, UnidentifiedImageError):
                    converted_path, output_format, width, height = await run_blocking_safely(
                        _extract_video_frame, raw_path
                    )
            else:
                converted_path, output_format, width, height = await run_blocking_safely(
                    _extract_video_frame, raw_path
                )
    finally:
        Path(raw_path).unlink(missing_ok=True)
        if reference_path:
            Path(reference_path).unlink(missing_ok=True)
    derived_post = _derived_post(post, output_format, width, height, "original_frame", post.image_url)
    if original_format in VIDEO_FORMATS:
        metadata = dict(derived_post.raw_metadata)
        marker = dict(metadata["_artist_collection_import"])
        marker["frame_selection"] = frame_selection
        if frame_selection == "artstation_cover_match":
            marker["frame_reference_url"] = post.preview_url
        metadata["_artist_collection_import"] = marker
        derived_post = derived_post.model_copy(update={"raw_metadata": metadata})
    return converted_path, derived_post, "original_frame"


def _finish_original_frame(
    post: RemotePost,
    raw_path: str,
    converted_path: str,
    output_format: str,
    width: int,
    height: int,
) -> tuple[str, RemotePost, str]:
    Path(raw_path).unlink(missing_ok=True)
    derived_post = _derived_post(post, output_format, width, height, "original_frame", post.image_url)
    return converted_path, derived_post, "original_frame"


def _safe_archive_media_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or "\x00" in normalized
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
        or any(part == "__MACOSX" or part.startswith(".") for part in parts)
    ):
        return False
    return normalized_format(Path(normalized).suffix) in PIL_IMAGE_FORMATS | VIDEO_FORMATS


def _natural_name_key(name: str) -> list[tuple[int, object]]:
    # Tagged tuples keep Python from comparing integers to strings when names
    # mix numeric and non-numeric chunks.
    return [
        (0, int(chunk)) if chunk.isdigit() else (1, chunk.casefold())
        for chunk in re.split(r"(\d+)", name.replace("\\", "/"))
    ]


def _middle_out(items: list) -> list:
    """Try the middle frame first, then neighboring frames symmetrically."""
    if not items:
        return []
    middle = len(items) // 2
    indices = [middle]
    for distance in range(1, len(items)):
        right = middle + distance
        left = middle - distance
        if right < len(items):
            indices.append(right)
        if left >= 0:
            indices.append(left)
    return [items[index] for index in indices]


def _validate_still(path: str) -> tuple[str, int, int, bool]:
    with Image.open(path) as source:
        source.seek(0)
        source.load()
        output_format = normalized_format(source.format or Path(path).suffix)
        width, height = source.size
        if output_format not in PIL_IMAGE_FORMATS or width < 1 or height < 1:
            raise ValueError("Archive member is not a usable image")
        animated = bool(getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1)
    return output_format, width, height, animated


def _normalize_extracted_archive_frame(frame_path: str) -> tuple[str, str, int, int]:
    if normalized_format(Path(frame_path).suffix) in VIDEO_FORMATS:
        if not video_decoder_status()["available"]:
            raise ValueError("Archive contains video, but the video decoder is unavailable")
        converted_path, output_format, width, height = _extract_video_frame(frame_path)
        Path(frame_path).unlink(missing_ok=True)
        return converted_path, output_format, width, height
    output_format, width, height, animated = _validate_still(frame_path)
    if animated or output_format == "gif":
        converted_path, output_format, width, height = _convert_first_frame(
            frame_path, representative=True, lossless=True
        )
        Path(frame_path).unlink(missing_ok=True)
        return converted_path, output_format, width, height
    return frame_path, output_format, width, height


def _copy_archive_member(source, destination: str) -> None:
    total = 0
    with open(destination, "wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_FRAME_SIZE:
                raise ValueError("Archive frame exceeds the 512 MiB safety limit")
            output.write(chunk)


def _extract_zip_sequence_frames(archive_path: str) -> list[tuple[str, str, int, float]]:
    extracted: list[str] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES or sum(info.file_size for info in infos) > MAX_ARCHIVE_SIZE:
                raise ValueError("Archive exceeds entry-count or expanded-size safety limit")
            eligible = [info for info in infos if (
                not info.is_dir() and not (info.flag_bits & 0x1)
                and ((info.external_attr >> 16) & 0o170000) != stat.S_IFLNK
                and info.file_size <= MAX_ARCHIVE_FRAME_SIZE
                and _safe_archive_media_name(info.filename)
            )]
            images = [info for info in eligible if normalized_format(Path(info.filename).suffix) in PIL_IMAGE_FORMATS]
            videos = [info for info in eligible if normalized_format(Path(info.filename).suffix) in VIDEO_FORMATS]
            candidates = _best_zip_still_sequence(archive, images) if images else videos
            candidates.sort(key=lambda info: _natural_name_key(info.filename))
            if not candidates:
                raise ValueError("Archive contains no supported image or video frames")
            positions = _spaced_frame_indices(len(candidates)) if images else [len(candidates) // 2]
            result = []
            for index in positions:
                info = candidates[index]
                path = _new_temp_path(Path(info.filename).suffix[:16])
                extracted.append(path)
                with archive.open(info, "r") as source:
                    _copy_archive_member(source, path)
                if info in images:
                    _validate_still(path)
                elif Path(path).stat().st_size < 1:
                    raise ValueError("Archive video member is empty")
                result.append((path, info.filename, len(candidates), index / max(1, len(candidates) - 1)))
            return result
    except BaseException:
        for path in extracted:
            Path(path).unlink(missing_ok=True)
        raise


def _extract_rar_sequence_frames(archive_path: str) -> list[tuple[str, str, int, float]]:
    status = archive_decoder_status()["rar"]
    if not status["available"]:
        raise StaticPreviewUnavailable("A RAR decoder is not available")
    executable = status["executable"]
    listing = subprocess.run([executable, "-tf", archive_path], capture_output=True, text=True, timeout=60, check=False)
    if listing.returncode:
        raise ValueError(listing.stderr.strip() or "RAR archive could not be listed")
    names = listing.stdout.splitlines()
    if len(names) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("Archive contains too many entries")
    images = sorted([name for name in names if _safe_archive_media_name(name) and normalized_format(Path(name).suffix) in PIL_IMAGE_FORMATS], key=_natural_name_key)
    videos = sorted([name for name in names if _safe_archive_media_name(name) and normalized_format(Path(name).suffix) in VIDEO_FORMATS], key=_natural_name_key)
    candidates = images or videos
    if not candidates:
        raise ValueError("Archive contains no supported image or video frames")
    positions = _spaced_frame_indices(len(candidates)) if images else [len(candidates) // 2]
    extracted = []
    try:
        result = []
        for index in positions:
            name = candidates[index]
            path = _new_temp_path(Path(name).suffix[:16])
            extracted.append(path)
            _extract_rar_member(executable, archive_path, name, path)
            if images:
                _validate_still(path)
            elif Path(path).stat().st_size < 1:
                raise ValueError("Archive video member is empty")
            result.append((path, name, len(candidates), index / max(1, len(candidates) - 1)))
        return result
    except BaseException:
        for path in extracted:
            Path(path).unlink(missing_ok=True)
        raise


def _extract_zip_frame(archive_path: str) -> tuple[str, str, int]:
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Archive contains too many entries")
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_SIZE:
            raise ValueError("Archive expands beyond the 2 GiB safety limit")

        image_candidates = []
        video_candidates = []
        for info in infos:
            unix_mode = (info.external_attr >> 16) & 0o170000
            if (
                info.is_dir()
                or info.flag_bits & 0x1
                or unix_mode == stat.S_IFLNK
                or info.file_size > MAX_ARCHIVE_FRAME_SIZE
                or not _safe_archive_media_name(info.filename)
            ):
                continue
            if normalized_format(Path(info.filename).suffix) in PIL_IMAGE_FORMATS:
                image_candidates.append(info)
            else:
                video_candidates.append(info)
        # A sequence of original still frames is preferable to decoding a
        # contained video. Fall back to video only when no stills exist.
        candidates = _best_zip_still_sequence(archive, image_candidates) if image_candidates else video_candidates
        candidates.sort(key=lambda info: _natural_name_key(info.filename))
        if not candidates:
            raise ValueError("Archive contains no supported image or video frames")

        for info in _middle_out(candidates):
            output_path = _new_temp_path(Path(info.filename).suffix[:16])
            try:
                with archive.open(info, "r") as source:
                    _copy_archive_member(source, output_path)
                if normalized_format(Path(info.filename).suffix) in PIL_IMAGE_FORMATS:
                    _validate_still(output_path)
                elif Path(output_path).stat().st_size < 1:
                    raise ValueError("Archive video member is empty")
                return output_path, info.filename, len(candidates)
            except (OSError, RuntimeError, UnidentifiedImageError, ValueError):
                Path(output_path).unlink(missing_ok=True)
        raise ValueError("Archive image entries could not be decoded")


def _best_zip_still_sequence(archive: zipfile.ZipFile, candidates: list[zipfile.ZipInfo]) -> list[zipfile.ZipInfo]:
    """Prefer a repeated high-resolution frame sequence over embedded thumbs."""
    by_dimensions: dict[tuple[int, int], list[zipfile.ZipInfo]] = {}
    for info in candidates:
        try:
            with archive.open(info, "r") as source, Image.open(source) as image:
                width, height = image.size
                if width < 1 or height < 1:
                    continue
            by_dimensions.setdefault((width, height), []).append(info)
        except (OSError, RuntimeError, UnidentifiedImageError, ValueError, zipfile.BadZipFile):
            continue
    if not by_dimensions:
        return candidates
    # count*area favors a real sequence over a single cover image, while also
    # choosing originals over an equal-sized thumbnail sequence.
    return max(
        by_dimensions.items(),
        key=lambda item: (
            len(item[1]) * item[0][0] * item[0][1],
            len(item[1]),
            item[0][0] * item[0][1],
        ),
    )[1]


def _extract_rar_frame(archive_path: str) -> tuple[str, str, int]:
    status = archive_decoder_status()["rar"]
    if not status["available"]:
        raise StaticPreviewUnavailable("A RAR decoder is not available")
    executable = status["executable"]
    listing = subprocess.run(
        [executable, "-tf", archive_path],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if listing.returncode != 0:
        raise ValueError(listing.stderr.strip() or "RAR archive could not be listed")
    names = listing.stdout.splitlines()
    if len(names) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("Archive contains too many entries")
    media_names = {name for name in names if _safe_archive_media_name(name)}
    image_candidates = [name for name in media_names if normalized_format(Path(name).suffix) in PIL_IMAGE_FORMATS]
    video_candidates = [name for name in media_names if normalized_format(Path(name).suffix) in VIDEO_FORMATS]
    candidates = sorted(image_candidates or video_candidates, key=_natural_name_key)
    if not candidates:
        raise ValueError("Archive contains no supported image or video frames")

    for member_name in _middle_out(candidates):
        output_path = _new_temp_path(Path(member_name).suffix[:16])
        try:
            _extract_rar_member(executable, archive_path, member_name, output_path)
            if normalized_format(Path(member_name).suffix) in PIL_IMAGE_FORMATS:
                _validate_still(output_path)
            elif Path(output_path).stat().st_size < 1:
                raise ValueError("Archive video member is empty")
            return output_path, member_name, len(candidates)
        except (OSError, subprocess.SubprocessError, UnidentifiedImageError, ValueError):
            Path(output_path).unlink(missing_ok=True)
    raise ValueError("Archive image entries could not be decoded")


def _extract_rar_member(executable: str, archive_path: str, member_name: str, output_path: str) -> None:
    """Stream one RAR member through bsdtar with byte and time limits."""
    process = None
    timed_out = False

    def terminate_for_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        if process and process.poll() is None:
            process.kill()

    with tempfile.TemporaryFile() as error_output, open(output_path, "wb") as output:
        process = subprocess.Popen(
            [executable, "-xOf", archive_path, "--", member_name],
            stdout=subprocess.PIPE,
            stderr=error_output,
        )
        timer = threading.Timer(180, terminate_for_timeout)
        timer.daemon = True
        timer.start()
        total = 0
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_FRAME_SIZE:
                    process.kill()
                    raise ValueError("RAR frame exceeds the 512 MiB safety limit")
                output.write(chunk)
            return_code = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        error_output.seek(0)
        error_message = error_output.read(4096).decode("utf-8", errors="replace").strip()
    if timed_out:
        raise ValueError("RAR frame extraction timed out")
    if return_code != 0:
        raise ValueError(error_message or "RAR member could not be extracted")


def _derived_post(post: RemotePost, output_format: str, width: int, height: int, source: str, image_url: str) -> RemotePost:
    metadata = dict(post.raw_metadata)
    metadata["_artist_collection_import"] = {
        "derived_from_preview": source == "provider_preview",
        "derived_media_source": source,
        "original_media_format": post.format or None,
        "original_media_url": post.image_url,
        "preview_url": post.preview_url,
        "frame_source_url": image_url,
        "converted_format": output_format,
        "converted_width": width,
        "converted_height": height,
    }
    derived_post = post.model_copy(update={
        "format": output_format,
        "width": width,
        "height": height,
        "md5": None,
        "raw_metadata": metadata,
    })
    return derived_post


def _convert_first_frame(
    raw_path: str,
    representative: bool = False,
    lossless: bool = False,
) -> tuple[str, str, int, int]:
    output_path: str | None = None
    try:
        with Image.open(raw_path) as source:
            if representative and getattr(source, "n_frames", 1) > 1:
                source.seek(max(0, source.n_frames // 2))
            else:
                source.seek(0)
            frame = ImageOps.exif_transpose(source)
            frame.load()
            width, height = frame.size
            if width < 1 or height < 1:
                raise ValueError("Preview image has invalid dimensions")
            has_alpha = frame.mode in {"RGBA", "LA"} or (frame.mode == "P" and "transparency" in frame.info)
            if lossless or has_alpha:
                output_format = "png"
                output_path = _new_temp_path(".png")
                converted = frame.convert("RGBA" if has_alpha else "RGB")
                # This is a short-lived intermediate that is decoded again by
                # the final training-image encoder. Fast, lossless PNG output
                # avoids spending CPU on compression that is immediately
                # discarded.
                converted.save(output_path, "PNG", compress_level=0)
            else:
                output_format = "jpeg"
                output_path = _new_temp_path(".jpg")
                converted = frame.convert("RGB")
                converted.save(output_path, "JPEG", quality=95, optimize=False)
            converted.close()
            if frame is not source:
                frame.close()
        return output_path, output_format, width, height
    except Exception:
        if output_path:
            Path(output_path).unlink(missing_ok=True)
        raise


def _extract_video_frame(raw_path: str) -> tuple[str, str, int, int]:
    status = video_decoder_status()
    if not status["available"]:
        raise StaticPreviewUnavailable("A video decoder is not installed")
    output_path = _new_temp_path(".png")
    command = [
        status["executable"], "-hide_banner", "-loglevel", "error", "-y",
        "-i", raw_path, "-vf", "thumbnail=100", "-frames:v", "1",
        "-compression_level", "0", output_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0:
        Path(output_path).unlink(missing_ok=True)
        raise ValueError(result.stderr.strip() or "FFmpeg could not extract a video frame")
    try:
        with Image.open(output_path) as image:
            image.load()
            width, height = image.size
    except Exception:
        Path(output_path).unlink(missing_ok=True)
        raise
    return output_path, "png", width, height


def _extract_video_frames(raw_path: str, reference_path: str | None = None) -> list[tuple[str, str, int, int, float]]:
    """Sample the full original and retain three temporally spread, nonidentical frames."""
    capture = cv2.VideoCapture(raw_path)
    written: list[str] = []
    try:
        if not capture.isOpened():
            return _extract_video_frames_ffmpeg(raw_path, reference_path)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count < 1:
            return _extract_video_frames_ffmpeg(raw_path, reference_path)
        sample_count = min(64 if reference_path else 32, frame_count)
        indices = sorted({int(index) for index in np.linspace(0, frame_count - 1, sample_count)})
        reference = cv2.imread(reference_path, cv2.IMREAD_COLOR) if reference_path else None
        aspect = reference.shape[1] / reference.shape[0] if reference is not None else None
        reference_signature = _visual_match_signature(reference, aspect) if reference is not None else None
        candidates: list[tuple[int, np.ndarray, float]] = []
        for index in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            available, frame = capture.read()
            if available and frame is not None:
                signature = cv2.resize(frame, (64, 64), interpolation=cv2.INTER_AREA)
                score = _visual_match_score(reference_signature, frame, aspect) if reference_signature is not None else 0.0
                candidates.append((index, signature, score))
        if len(candidates) < min(VIDEO_FRAME_COUNT, frame_count):
            return _extract_video_frames_ffmpeg(raw_path, reference_path)

        if reference_signature is not None:
            anchor = min(candidates, key=lambda item: item[2])
            chosen = [anchor]
            targets = (0.2, 0.8)
        else:
            chosen = []
            targets = (0.2, 0.5, 0.8)

        def different(candidate: tuple[int, np.ndarray, float]) -> bool:
            return all(np.mean(cv2.absdiff(candidate[1], selected[1])) > 1.5 for selected in chosen)

        for fraction in targets:
            target = (frame_count - 1) * fraction
            for candidate in sorted(candidates, key=lambda item: abs(item[0] - target)):
                if all(candidate[0] != selected[0] for selected in chosen) and different(candidate):
                    chosen.append(candidate)
                    break
        # Very short clips or a static loop can legitimately yield fewer than
        # three unique stills; never fabricate duplicate training examples.
        for candidate in candidates:
            if len(chosen) >= VIDEO_FRAME_COUNT:
                break
            if all(candidate[0] != selected[0] for selected in chosen) and different(candidate):
                chosen.append(candidate)

        result = []
        for index, _, _ in sorted(chosen[:VIDEO_FRAME_COUNT], key=lambda item: item[0]):
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            available, frame = capture.read()
            if not available or frame is None:
                raise ValueError(f"Could not reread selected video frame {index}")
            path = _new_temp_path(".png")
            written.append(path)
            if not cv2.imwrite(path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 0]):
                raise ValueError("Could not write a decoded video frame")
            height, width = frame.shape[:2]
            result.append((path, "png", width, height, index / max(1, frame_count - 1)))
        return result
    except BaseException:
        for path in written:
            Path(path).unlink(missing_ok=True)
        raise
    finally:
        capture.release()


def _extract_video_frames_ffmpeg(raw_path: str, reference_path: str | None) -> list[tuple[str, str, int, int, float]]:
    """Fallback for originals whose frame count OpenCV cannot index."""
    executable = video_decoder_status()["executable"]
    probe = subprocess.run(
        [executable, "-hide_banner", "-i", raw_path],
        capture_output=True, text=True, timeout=30, check=False,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if not match:
        raise ValueError("Original video exposes neither an indexed frame count nor a duration")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise ValueError("Original video has no positive duration")
    paths: list[str] = []
    try:
        reference = cv2.imread(reference_path, cv2.IMREAD_COLOR) if reference_path else None
        aspect = reference.shape[1] / reference.shape[0] if reference is not None else None
        reference_signature = _visual_match_signature(reference, aspect) if reference is not None else None
        candidates = []
        for fraction in (0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9):
            path = _new_temp_path(".png")
            paths.append(path)
            command = [
                executable, "-hide_banner", "-loglevel", "error", "-y", "-ss",
                f"{duration * fraction:.4f}", "-i", raw_path, "-frames:v", "1",
                "-compression_level", "0", path,
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
            if result.returncode:
                continue
            frame = cv2.imread(path, cv2.IMREAD_COLOR)
            if frame is not None:
                small_color = cv2.resize(frame, (64, 64), interpolation=cv2.INTER_AREA)
                score = _visual_match_score(reference_signature, frame, aspect) if reference_signature is not None else 0.0
                candidates.append((fraction, path, small_color, score))
        if not candidates:
            raise ValueError("FFmpeg could not decode any original-resolution video frames")
        if reference_signature is not None:
            chosen = [min(candidates, key=lambda item: item[3])]
            targets = (0.2, 0.8)
        else:
            chosen = []
            targets = (0.2, 0.5, 0.8)
        for target in targets:
            for candidate in sorted(candidates, key=lambda item: abs(item[0] - target)):
                if all(candidate[1] != item[1] and np.mean(cv2.absdiff(candidate[2], item[2])) > 1.5 for item in chosen):
                    chosen.append(candidate)
                    break
        keep = {item[1] for item in chosen[:VIDEO_FRAME_COUNT]}
        for path in paths:
            if path not in keep:
                Path(path).unlink(missing_ok=True)
        result = []
        for fraction, path, _, _ in sorted(chosen[:VIDEO_FRAME_COUNT], key=lambda item: item[0]):
            frame = cv2.imread(path, cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("Could not read an FFmpeg-extracted video frame")
            height, width = frame.shape[:2]
            result.append((path, "png", width, height, fraction))
        return result
    except BaseException:
        for path in paths:
            Path(path).unlink(missing_ok=True)
        raise


def _center_crop_to_aspect(image: np.ndarray, target_aspect: float) -> np.ndarray:
    height, width = image.shape[:2]
    if width < 1 or height < 1 or target_aspect <= 0:
        return image
    aspect = width / height
    if aspect > target_aspect:
        crop_width = max(1, round(height * target_aspect))
        left = max(0, (width - crop_width) // 2)
        return image[:, left:left + crop_width]
    if aspect < target_aspect:
        crop_height = max(1, round(width / target_aspect))
        top = max(0, (height - crop_height) // 2)
        return image[top:top + crop_height, :]
    return image


def _visual_match_signature(image: np.ndarray, target_aspect: float) -> tuple[np.ndarray, np.ndarray]:
    cropped = _center_crop_to_aspect(image, target_aspect)
    resized = cv2.resize(cropped, (64, 64), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    dct = cv2.dct(np.float32(gray))[:8, :8]
    phash = dct > np.median(dct[1:, :])
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    cv2.normalize(histogram, histogram)
    return phash, histogram


def _visual_match_score(
    reference_signature: tuple[np.ndarray, np.ndarray],
    frame: np.ndarray,
    target_aspect: float,
) -> float:
    phash, histogram = _visual_match_signature(frame, target_aspect)
    reference_hash, reference_histogram = reference_signature
    hash_distance = float(np.count_nonzero(phash != reference_hash))
    color_distance = float(cv2.compareHist(reference_histogram, histogram, cv2.HISTCMP_BHATTACHARYYA))
    return hash_distance + color_distance * 32.0


def _extract_artstation_cover_frame(
    raw_path: str,
    reference_path: str,
) -> tuple[str, str, int, int]:
    """Select the original-resolution video frame closest to the project cover."""
    reference = cv2.imread(reference_path, cv2.IMREAD_COLOR)
    if reference is None or reference.shape[0] < 1 or reference.shape[1] < 1:
        raise ValueError("ArtStation cover reference could not be decoded")
    target_aspect = reference.shape[1] / reference.shape[0]
    reference_signature = _visual_match_signature(reference, target_aspect)

    capture = cv2.VideoCapture(raw_path)
    try:
        if not capture.isOpened():
            raise ValueError("ArtStation original video could not be opened")
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count < 1:
            raise ValueError("ArtStation original video did not report a frame count")
        sample_count = min(64, frame_count)
        indices = sorted({int(index) for index in np.linspace(0, frame_count - 1, sample_count)})
        best_frame: np.ndarray | None = None
        best_score = float("inf")
        for index in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            available, frame = capture.read()
            if not available or frame is None:
                continue
            score = _visual_match_score(reference_signature, frame, target_aspect)
            if score < best_score:
                best_score = score
                best_frame = frame.copy()
        if best_frame is None:
            raise ValueError("No decodable frames were found in the ArtStation original video")
    finally:
        capture.release()

    output_path = _new_temp_path(".png")
    if not cv2.imwrite(output_path, best_frame, [cv2.IMWRITE_PNG_COMPRESSION, 0]):
        Path(output_path).unlink(missing_ok=True)
        raise ValueError("Could not write the matched ArtStation video frame")
    height, width = best_frame.shape[:2]
    return output_path, "png", width, height


def _new_temp_path(suffix: str) -> str:
    from app.services.workspace import scratch_directory

    temporary = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=scratch_directory())
    path = temporary.name
    temporary.close()
    return path
