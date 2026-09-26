from app.services.diagnostics import emit, context, job_event
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

import httpx

from app.db import get_connection
from app.providers.registry import create_provider
from app.services.images import ImageService
from app.services.media import ARCHIVE_FORMATS, StaticPreviewUnavailable, VIDEO_FORMATS, download_importable_image, download_importable_images, ensure_image_meets_folder_quality, normalized_format, run_blocking_safely
from app.services.queries import provider_query_for_folder, validate_collection_query
from app.services.settings import get_parallel_workers
from app.models import SyncResult

SUPPORTED_IMAGE_FORMATS = {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "avif"}
MAX_RETRY_ATTEMPTS = 3


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, OSError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    message = str(exc).lower()
    return bool(re.search(r"http (429|5\d\d)", message))


async def _single_importable_image(provider, post, progress):
    return [await download_importable_image(provider, post, progress=progress)]


class SyncService:
    """Service for syncing collections with providers."""

    def __init__(self, library_path: Path):
        self.library_path = library_path
        self.image_service = ImageService(library_path)

    async def _process_post(
        self,
        provider,
        post,
        collection_id: int,
        collection_filters: dict,
        allowed_formats: set[str],
        file_callback=None,
    ) -> dict:
        for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
            result = await self._process_post_once(
                provider, post, collection_id, collection_filters, allowed_formats, file_callback
            )
            if result.get("status") != "error" or not result.get("retryable") or attempt == MAX_RETRY_ATTEMPTS:
                result.pop("retryable", None)
                return result
            delay = 2 ** (attempt - 1)
            if file_callback:
                file_callback({
                    "stage": "retrying", "remote_id": post.remote_id, "format": post.format,
                    "retry_attempt": attempt, "retry_in_seconds": delay,
                })
            await asyncio.sleep(delay)
        raise RuntimeError("unreachable retry state")

    async def _process_post_once(
        self,
        provider,
        post,
        collection_id: int,
        collection_filters: dict,
        allowed_formats: set[str],
        file_callback=None,
    ) -> dict:
        tmp_paths: list[str] = []
        file_progress = {"stage": "starting", "remote_id": post.remote_id, "format": post.format}

        def report_file(update: dict) -> None:
            file_progress.update(update)
            if file_callback:
                file_callback(dict(file_progress))

        try:
            from app.services.settings import get_processing_settings
            policy = get_processing_settings()
            legacy_output_filter = policy["enabled"] and policy["output_format"] == "auto"
            post_format = normalized_format(post.format)
            static_original_allowed = post_format in SUPPORTED_IMAGE_FORMATS
            if legacy_output_filter and not static_original_allowed and not ({"jpg", "webp"} & allowed_formats):
                return {"status": "skipped", "message": f"Skipped {post.remote_id}: folder does not allow JPEG/WebP training output", "transfer": None}

            conn = get_connection()
            rejected = conn.execute(
                "SELECT 1 FROM rejected_post WHERE provider = ? AND remote_id = ?",
                (post.provider, post.remote_id),
            ).fetchone()
            conn.close()
            if rejected:
                return {"status": "skipped", "message": f"Skipped rejected post {post.remote_id}", "transfer": None}

            existing_source_id = await run_blocking_safely(
                self.image_service.check_existing_by_source,
                post.provider,
                post.remote_id,
                collection_id,
            )
            motion_candidate = post_format in VIDEO_FORMATS | ARCHIVE_FORMATS | {"gif", "webp", "png", "avif"}
            if existing_source_id and post_format not in VIDEO_FORMATS | ARCHIVE_FORMATS | {"gif"}:
                # Ordinary stills, including WebP/PNG, keep the cheap exact
                # source fast path. Newly imported animations use numbered IDs.
                await run_blocking_safely(self.image_service._add_source_record, existing_source_id, post)
                return {
                    "status": "skipped", "message": f"Already have source {post.remote_id}; skipped media download",
                    "transfer": None, "image_id": existing_source_id,
                }
            frame_ids, frame_count = await run_blocking_safely(
                self.image_service.existing_frames, post.provider, post.remote_id, collection_id,
            ) if motion_candidate else ([], 0)
            if frame_count and all(frame_ids[:frame_count]):
                return {"status": "skipped", "message": f"Already have all {frame_count} distinct frame(s) for {post.remote_id}; skipped media download", "transfer": None}
            existing_id = self.image_service.check_existing_by_hash(
                md5=post.md5, folder_id=collection_id
            ) if static_original_allowed else None
            if existing_id:
                await run_blocking_safely(self.image_service._link_to_collection, existing_id, collection_id)
                await run_blocking_safely(self.image_service._add_source_record, existing_id, post)
                return {"status": "skipped", "message": f"Already have post {post.remote_id}", "transfer": None}

            from app.services.server import require_space
            await run_blocking_safely(require_space, self.library_path)
            items = await (
                download_importable_images(provider, post, progress=report_file)
                if motion_candidate else
                _single_importable_image(provider, post, report_file)
            )
            tmp_paths = [path for path, _, _ in items]
            new_count = 0
            existing_count = 0
            rejected_frames = 0
            image_id = None
            for path, ingest_post, derived_media_source in items:
                prior_id = await run_blocking_safely(
                    self.image_service.check_existing_by_source,
                    ingest_post.provider, ingest_post.remote_id, collection_id,
                )
                if prior_id:
                    image_id = prior_id
                    existing_count += 1
                    continue
                try:
                    await run_blocking_safely(ensure_image_meets_folder_quality, path, collection_filters)
                except StaticPreviewUnavailable as exc:
                    emit("media.filtered", "Media skipped by quality requirements", "INFO", reason=str(exc))
                    rejected_frames += 1
                    continue
                converted_format = normalized_format(ingest_post.format)
                training_format = "jpg" if converted_format == "jpg" else "webp"
                if legacy_output_filter and training_format not in allowed_formats:
                    rejected_frames += 1
                    continue
                image_id, is_new = await run_blocking_safely(
                    self.image_service.ingest_image, path, ingest_post, collection_id,
                    derived_from_preview=derived_media_source == "provider_preview",
                    original_media_format=post.format, original_media_url=post.image_url,
                    derived_media_source=derived_media_source,
                )
                if is_new:
                    new_count += 1
                else:
                    existing_count += 1
            file_progress.update({"stage": "complete", "percent": 100, "eta_seconds": 0})
            return {
                "status": "new" if new_count else "skipped",
                "new_images": new_count,
                "message": f"Processed {post.remote_id}: {new_count} new frame(s), {existing_count} existing, {rejected_frames} filtered" if len(items) > 1 else f"Downloaded processed image and tags for {post.remote_id}" if new_count else f"Skipped or already had {post.remote_id}",
                "transfer": dict(file_progress),
                "image_id": image_id,
            }
        except StaticPreviewUnavailable as exc:
            return {"status": "skipped", "message": f"Skipped post {post.remote_id}: {exc}", "transfer": None}
        except Exception as exc:
            emit("sync.post_failed", "Post processing failed", "ERROR", error=exc, remote_id=post.remote_id, folder_id=collection_id)
            return {"status": "error", "message": f"Post {post.remote_id}: {exc}", "transfer": None, "retryable": _is_retryable_error(exc)}
        finally:
            for path in tmp_paths:
                Path(path).unlink(missing_ok=True)

    async def sync_collection(
        self,
        collection_id: int,
        provider_name: str,
        limit: int = 20,
        progress=None,
        sort: str = "latest",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> SyncResult:
        """Sync a single collection from a provider."""
        start_time = datetime.now(timezone.utc)

        conn = get_connection()
        cursor = conn.cursor()

        # Get collection details
        cursor.execute("SELECT * FROM collection WHERE id = ?", (collection_id,))
        collection = cursor.fetchone()
        if not collection:
            conn.close()
            raise ValueError(f"Folder {collection_id} not found")

        collection = dict(collection)
        # Get source info
        cursor.execute("""
            SELECT last_cursor, backfill_cursor, enabled, query_override FROM collection_source
            WHERE collection_id = ? AND provider = ?
        """, (collection_id, provider_name))
        source_row = cursor.fetchone()

        if not source_row:
            conn.close()
            raise ValueError(f"Provider {provider_name} not configured for folder {collection_id}")

        last_cursor, backfill_cursor, enabled, query_override = source_row[0], source_row[1], source_row[2], source_row[3]
        validate_collection_query(query_override or collection['query'], collection.get('type', 'artist'), provider_name)
        query = provider_query_for_folder(query_override or collection['query'], collection.get('type', 'artist'))

        if not enabled:
            conn.close()
            return SyncResult(
                collection_id=collection_id,
                provider=provider_name,
                new_images=0,
                skipped=0,
                errors=0,
                duration_seconds=0
            )

        conn.close()

        # Initialize provider
        provider = create_provider(provider_name)
        provider.literal_query = collection.get('type') != 'artist'
        if hasattr(provider, "search_progress"):
            provider.search_progress = progress

        new_images = 0
        skipped = 0
        errors = 0

        try:
            async def search_with_retry(search_query, search_cursor, search_limit, search_sort, search_from=None, search_to=None):
                for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
                    try:
                        if progress:
                            progress({
                                "phase": "search", "total": 0, "completed": 0,
                                "message": f"Searching {provider_name} for matching posts...",
                                "_transient": True,
                            })
                        return await provider.search(
                            search_query, search_cursor, search_limit, sort=search_sort,
                            date_from=search_from, date_to=search_to,
                        )
                    except Exception as exc:
                        emit("sync.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
                        if attempt == MAX_RETRY_ATTEMPTS or not _is_retryable_error(exc):
                            raise
                        delay = 2 ** (attempt - 1)
                        if progress:
                            progress({"phase": "retry", "message": f"Search failed; retry {attempt}/{MAX_RETRY_ATTEMPTS} in {delay}s: {exc}"})
                        await asyncio.sleep(delay)
                return [], None

            posts = []
            proposed_watermark = last_cursor
            proposed_backfill = backfill_cursor
            incremental_mode = sort == "latest" and not date_from and not date_to
            if not incremental_mode:
                # Historical/date-filtered browsing must never consume the
                # automatic incremental watermark or backfill position.
                posts, _ = await search_with_retry(query, None, limit, sort, date_from, date_to)
            elif getattr(provider, "ordered_feed", False):
                # Profile/timeline extractors use a stable numeric offset, not
                # a monotonic post ID. Always reserve some capacity for the
                # newest items, then continue the older-catalog backfill.
                if backfill_cursor:
                    newest_limit = max(1, min(limit, limit // 3))
                    newest_posts, _ = await search_with_retry(query, None, newest_limit, "latest")
                    seen = set()
                    for post in newest_posts:
                        if post.remote_id not in seen:
                            seen.add(post.remote_id)
                            posts.append(post)
                    scan_cursor = backfill_cursor
                    for _ in range(8):
                        remaining = limit - len(posts)
                        if remaining <= 0 or not scan_cursor:
                            break
                        older_posts, next_cursor = await search_with_retry(
                            query, scan_cursor, remaining, "latest"
                        )
                        if next_cursor == scan_cursor:
                            raise RuntimeError(f"{provider_name} backfill did not advance from {scan_cursor}")
                        for post in older_posts:
                            if post.remote_id not in seen:
                                seen.add(post.remote_id)
                                posts.append(post)
                        proposed_backfill = next_cursor if older_posts else None
                        scan_cursor = proposed_backfill
                    if len(posts) < limit and proposed_backfill:
                        raise RuntimeError(
                            f"{provider_name} backfill repeated too many overlapping pages; "
                            "the sync will retry without advancing its cursor"
                        )
                    if proposed_backfill is None and len(posts) < limit:
                        # The saved offset reached the end. The reserved
                        # newest slice may be only a fraction of the user's
                        # requested amount, so refill from the beginning in
                        # this run instead of reporting a short success.
                        full_head, _ = await search_with_retry(query, None, limit, "latest")
                        seen = set()
                        refilled = []
                        for post in [*full_head, *posts]:
                            if post.remote_id not in seen:
                                seen.add(post.remote_id)
                                refilled.append(post)
                        posts = refilled[:limit]
                else:
                    posts, proposed_backfill = await search_with_retry(query, None, limit, "latest")
                if posts:
                    proposed_watermark = posts[0].remote_id
            elif last_cursor:
                # Fetch new IDs oldest-first from the committed watermark so a
                # limited page never skips newer posts that did not fit.
                newer_query = f"{query} id:>{last_cursor}"
                newer_posts, _ = await search_with_retry(newer_query, None, limit, "oldest")
                try:
                    watermark_number = int(last_cursor)
                    newer_posts = [post for post in newer_posts if int(post.remote_id) > watermark_number]
                except (TypeError, ValueError):
                    pass
                posts.extend(newer_posts)
                if newer_posts:
                    proposed_watermark = max(newer_posts, key=lambda post: int(post.remote_id)).remote_id
                remaining = limit - len(posts)
                if remaining > 0 and backfill_cursor:
                    older_posts, proposed_backfill = await search_with_retry(query, backfill_cursor, remaining, "latest")
                    seen = {post.remote_id for post in posts}
                    posts.extend(post for post in older_posts if post.remote_id not in seen)
            else:
                # Establish the high-water mark from the newest page. A legacy
                # older-page cursor remains available for the next backfill.
                newest_posts, first_backfill = await search_with_retry(query, None, limit, "latest")
                posts = newest_posts
                if newest_posts:
                    try:
                        proposed_watermark = max(newest_posts, key=lambda post: int(post.remote_id)).remote_id
                    except ValueError:
                        proposed_watermark = newest_posts[0].remote_id
                if not backfill_cursor:
                    proposed_backfill = first_backfill
            # Provider tag aliases are inconsistent: retry an artist name as a
            # plain canonical tag when the categorized query has no results.
            if not posts and collection.get('type') == 'artist' and ':' not in (query_override or collection['query']):
                fallback_query = (query_override or collection['query']).replace(' ', '_')
                if fallback_query != query:
                    posts, _ = await search_with_retry(fallback_query, None, limit, sort, date_from, date_to)
            if progress:
                progress({"phase": "search", "total": len(posts), "completed": 0, "message": f"Found {len(posts)} posts"})

            collection_filters = json.loads(collection.get("filters") or "{}")
            allowed_formats = {
                "jpg" if str(value).lower().lstrip(".") in {"jpg", "jpeg", "jpe"}
                else str(value).lower().lstrip(".")
                for value in collection_filters.get("allowed_formats", SUPPORTED_IMAGE_FORMATS)
            }
            workers = get_parallel_workers()
            semaphore = asyncio.Semaphore(workers)
            active_files: dict[str, dict] = {}
            completed_count = 0

            async def process_one(index: int, post) -> None:
                nonlocal completed_count, new_images, skipped, errors
                async with semaphore:
                    key = f"{post.provider}:{post.remote_id}:{index}"

                    def report_file(update: dict) -> None:
                        active_files[key] = dict(update)
                        if progress:
                            stage = str(update.get("stage", "downloading")).replace("_", " ").title()
                            progress({
                                "phase": "download",
                                "total": len(posts),
                                "completed": completed_count,
                                "workers": workers,
                                "message": f"{stage} {post.format or 'file'} for {post.remote_id}",
                                "current_file": dict(update),
                                "active_files": list(active_files.values()),
                                "_transient": True,
                            })

                    result = await self._process_post(
                        provider,
                        post,
                        collection_id,
                        collection_filters,
                        allowed_formats,
                        file_callback=report_file,
                    )
                    active_files.pop(key, None)
                    completed_count += 1
                    if result["status"] == "new":
                        new_images += result.get("new_images", 1)
                    elif result["status"] == "skipped":
                        skipped += 1
                    else:
                        errors += 1
                    if progress:
                        progress({
                            "phase": result["status"],
                            "total": len(posts),
                            "completed": completed_count,
                            "new_images": new_images,
                            "skipped": skipped,
                            "errors": errors,
                            "workers": workers,
                            "message": result["message"],
                            "current_file": result.get("transfer"),
                            "active_files": list(active_files.values()),
                        })

            if progress:
                progress({
                    "phase": "download",
                    "total": len(posts),
                    "completed": 0,
                    "workers": workers,
                    "active_files": [],
                    "message": f"Processing with {workers} parallel worker{'s' if workers != 1 else ''}",
                })
            await asyncio.gather(*(process_one(index, post) for index, post in enumerate(posts, 1)))

            # Commit both cursor dimensions only after every item on the page
            # finished without an error. Failed/canceled pages replay safely.
            if incremental_mode and errors == 0 and (proposed_watermark or proposed_backfill):
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE collection_source
                    SET last_cursor = ?, backfill_cursor = ?
                    WHERE collection_id = ? AND provider = ?
                """, (proposed_watermark, proposed_backfill, collection_id, provider_name))
                conn.commit()
                conn.close()

            # Update last_sync_at
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE collection
                SET last_sync_at = ?
                WHERE id = ?
            """, (datetime.now(timezone.utc).isoformat(), collection_id))
            conn.commit()
            conn.close()

        finally:
            await provider.close()

        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        return SyncResult(
            collection_id=collection_id,
            provider=provider_name,
            new_images=new_images,
            skipped=skipped,
            errors=errors,
            duration_seconds=duration
        )

    async def sync_all_collections(self) -> List[SyncResult]:
        """Sync all enabled collections from all enabled sources."""
        conn = get_connection()
        cursor = conn.cursor()

        # Get all collection-source pairs
        cursor.execute("""
            SELECT c.id, cs.provider
            FROM collection c
            JOIN collection_source cs ON c.id = cs.collection_id
            WHERE c.enabled = 1 AND cs.enabled = 1
        """)

        pairs = [(row[0], row[1]) for row in cursor.fetchall()]
        conn.close()

        results = []
        for collection_id, provider in pairs:
            try:
                result = await self.sync_collection(collection_id, provider)
                results.append(result)
            except Exception as e:
                emit("service.notice", f"Error syncing collection {collection_id} from {provider}: {e}", "WARNING")
                results.append(SyncResult(
                    collection_id=collection_id,
                    provider=provider,
                    new_images=0,
                    skipped=0,
                    errors=1,
                    duration_seconds=0
                ))

        return results
