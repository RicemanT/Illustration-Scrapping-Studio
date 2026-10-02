"""Download one remote post into a folder: the shared core of selected imports.

Used by Import batches and Dataset Planner delivery so both follow the same
deduplication, motion-frame extraction, quality floor and provenance rules.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.models import RemotePost
from app.services.diagnostics import emit
from app.services.media import (
    ARCHIVE_FORMATS, StaticPreviewUnavailable, VIDEO_FORMATS, download_importable_image,
    download_importable_images, ensure_image_meets_folder_quality, normalized_format, run_blocking_safely,
)

STATIC_REUSE_EXCLUDED = VIDEO_FORMATS | ARCHIVE_FORMATS | {"gif"}
MOTION_CANDIDATES = VIDEO_FORMATS | ARCHIVE_FORMATS | {"gif", "webp", "png", "avif"}


async def ingest_post(provider, post: RemotePost, folder_id: int, folder_filters: dict, image_service, library: Path,
                      report_file: Callable[[dict], None], file_progress: dict) -> dict:
    """Return image_id, new_count, message and the final transfer state.

    Raises StaticPreviewUnavailable when every original-quality frame is
    unusable; other errors propagate to the caller's per-item handling.
    """
    fmt = normalized_format(post.format)
    image_id = await run_blocking_safely(image_service.check_existing_by_source, post.provider, post.remote_id, folder_id)
    if image_id and fmt not in STATIC_REUSE_EXCLUDED:
        await run_blocking_safely(image_service._add_source_record, image_id, post)
        return {"image_id": image_id, "new_count": 0, "transfer": None,
                "message": f"Already have source {post.remote_id}; skipped media download"}
    motion = fmt in MOTION_CANDIDATES
    frame_ids, frame_count = await run_blocking_safely(
        image_service.existing_frames, post.provider, post.remote_id, folder_id,
    ) if motion else ([], 0)
    if frame_count and all(frame_ids[:frame_count]):
        return {"image_id": frame_ids[0], "new_count": 0, "transfer": None,
                "message": f"Already have all {frame_count} distinct frame(s) for {post.remote_id}; skipped media download"}
    from app.services.server import require_space
    await run_blocking_safely(require_space, library)
    temp_paths: list[str] = []
    try:
        if motion:
            items = await download_importable_images(provider, post, progress=report_file)
        else:
            items = [await download_importable_image(provider, post, progress=report_file)]
        temp_paths = [path for path, _, _ in items]
        new_count = 0
        image_id = None
        for path, ingest_post_item, derived_media_source in items:
            prior_id = await run_blocking_safely(
                image_service.check_existing_by_source, ingest_post_item.provider, ingest_post_item.remote_id, folder_id,
            )
            if prior_id:
                image_id = image_id or prior_id
                continue
            try:
                await run_blocking_safely(ensure_image_meets_folder_quality, path, folder_filters)
            except StaticPreviewUnavailable as exc:
                emit("media.filtered", "Media skipped by quality requirements", "INFO", reason=str(exc))
                continue
            current_id, item_is_new = await run_blocking_safely(
                image_service.ingest_image, path, ingest_post_item, folder_id,
                derived_from_preview=derived_media_source == "provider_preview",
                original_media_format=post.format, original_media_url=post.image_url,
                derived_media_source=derived_media_source,
            )
            image_id = image_id or current_id
            new_count += int(item_is_new)
        file_progress.update({"stage": "complete", "percent": 100, "eta_seconds": 0})
        message = (f"Imported {new_count} frame(s) from {post.remote_id}" if new_count
                   else f"Already had or filtered all frames for {post.remote_id}")
        return {"image_id": image_id, "new_count": new_count, "transfer": dict(file_progress), "message": message}
    finally:
        for path in temp_paths:
            Path(path).unlink(missing_ok=True)
