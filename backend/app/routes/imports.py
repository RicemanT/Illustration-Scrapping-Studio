from app.services.diagnostics import emit, context, job_event
import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

from app.db import LIBRARY_PATH, get_connection
from app.models import ImportBatchCreate, ImportPreviewRequest
from app.providers.booru import BOORU_SITES, ProviderAuthenticationError, get_gelbooru_credentials
from app.providers.gallery_dl import GALLERY_DL_PROVIDERS, provider_ready
from app.providers.registry import create_provider, max_provider_limit, supported_provider
from app.services.images import ImageService
from app.services.media import StaticPreviewUnavailable
from app.services.post_ingest import ingest_post
from app.services.queries import provider_query_for_folder, validate_collection_query
from app.services.settings import get_parallel_workers

router = APIRouter()
_jobs: dict[str, dict] = {}
def _unique_remote_ids(remote_ids: list[str]) -> list[str]:
    """Keep batch ordering stable while ignoring blank/duplicate IDs."""
    return list(dict.fromkeys(str(remote_id).strip() for remote_id in remote_ids if str(remote_id).strip()))


def _new_job(job_id: str, batch_id: int, total: int) -> dict:
    return {
        "job_id": job_id,
        "batch_id": batch_id,
        "status": "queued",
        "progress": {"total": total, "completed": 0, "downloaded": 0, "skipped": 0, "errors": 0},
        "logs": [],
    }


@router.post("/preview")
async def import_preview(request: ImportPreviewRequest, http_request: Request = None):
    if request.date_from and request.date_to:
        try:
            if date.fromisoformat(request.date_from) > date.fromisoformat(request.date_to):
                raise HTTPException(status_code=422, detail="date_from must be before date_to")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date filters must use YYYY-MM-DD") from exc
    elif request.date_from or request.date_to:
        try:
            date.fromisoformat(request.date_from or request.date_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="date filters must use YYYY-MM-DD") from exc
    conn = get_connection()
    folder = conn.execute("SELECT type FROM collection WHERE id = ?", (request.folder_id,)).fetchone()
    conn.close()
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    try:
        validate_collection_query(request.query, folder[0], request.provider)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    provider_query = provider_query_for_folder(request.query, folder[0])
    try:
        provider = create_provider(request.provider)
        provider.literal_query = folder[0] != 'artist'
    except ProviderAuthenticationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        search_task = asyncio.create_task(provider.search(
            provider_query,
            request.cursor,
            min(request.limit, max_provider_limit(request.provider)),
            sort=request.sort,
            date_from=request.date_from,
            date_to=request.date_to,
        ))
        try:
            while not search_task.done():
                if http_request is not None and await http_request.is_disconnected():
                    search_task.cancel()
                    raise asyncio.CancelledError()
                await asyncio.wait({search_task}, timeout=0.2)
            posts, next_cursor = await search_task
        finally:
            if not search_task.done():
                search_task.cancel()
            await asyncio.gather(search_task, return_exceptions=True)
        conn = get_connection()
        items = []
        for post in posts:
            existing = conn.execute(
                "SELECT id FROM image WHERE folder_id = ? AND md5 = ?",
                (request.folder_id, post.md5),
            ).fetchone() if post.md5 else None
            if not existing:
                existing = conn.execute("""
                    SELECT image_source.image_id FROM image_source
                    JOIN image ON image.id = image_source.image_id
                    WHERE image.folder_id = ? AND provider = ? AND remote_id = ?
                    ORDER BY version DESC LIMIT 1
                """, (request.folder_id, post.provider, post.remote_id)).fetchone()
            items.append({**post.model_dump(), "already_imported": bool(existing), "existing_image_id": existing[0] if existing else None})
        conn.close()
        return {"items": items, "next_cursor": next_cursor, "total": len(items)}
    except ProviderAuthenticationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        emit("imports.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
        raise HTTPException(status_code=502, detail=f"{request.provider} preview failed: {exc}") from exc
    finally:
        await provider.close()


async def _run_import(job_id: str, batch_id: int, collection_id: int, provider_name: str, remote_ids: list[str]):
    job = _jobs[job_id]
    context.set({**context.get(), "job_id": job_id, "folder_id": collection_id, "provider": provider_name})
    emit("import.started", "Import batch started", batch_id=batch_id)
    from app.services.settings import get_processing_settings, processing_context
    job["processing"] = get_processing_settings()
    processing_context.set(job["processing"])
    provider = None
    image_service = ImageService(LIBRARY_PATH)
    try:
        conn = get_connection()
        folder_row = conn.execute("SELECT filters FROM collection WHERE id = ?", (collection_id,)).fetchone()
        conn.close()
        folder_filters = json.loads(folder_row[0] or "{}") if folder_row else {}
        provider = create_provider(provider_name)
        started_at = datetime.now(timezone.utc).isoformat()
        workers = get_parallel_workers()
        active_files: dict[str, dict] = {}
        job.update(status="running", started_at=started_at, progress={"total": len(remote_ids), "completed": 0, "downloaded": 0, "skipped": 0, "errors": 0, "workers": workers, "current_file": None, "active_files": []}, logs=[])
        conn = get_connection()
        conn.execute("UPDATE import_batch SET status = 'running', started_at = ?, job_id = ? WHERE id = ?", (started_at, job_id, batch_id))
        conn.commit()
        conn.close()
        semaphore = asyncio.Semaphore(workers)

        async def process_one(index: int, remote_id: str) -> None:
            key = f"{remote_id}:{index}"
            async with semaphore:
                conn = get_connection()
                cancelled = conn.execute("SELECT cancel_requested FROM import_batch WHERE id = ?", (batch_id,)).fetchone()
                conn.close()
                if cancelled and cancelled[0]:
                    job["status"] = "canceled"
                    return
                conn = get_connection()
                conn.execute("UPDATE import_item SET status = 'running' WHERE batch_id = ? AND remote_id = ?", (batch_id, remote_id))
                conn.commit()
                conn.close()
                try:
                    post = await provider.get_post(remote_id)
                    file_progress = {"stage": "starting", "remote_id": post.remote_id, "format": post.format}

                    def report_file(update: dict) -> None:
                        file_progress.update(update)
                        active_files[key] = dict(file_progress)
                        job["progress"]["current_file"] = dict(file_progress)
                        job["progress"]["active_files"] = list(active_files.values())

                    result = await ingest_post(provider, post, collection_id, folder_filters, image_service,
                                               LIBRARY_PATH, report_file, file_progress)
                    image_id, new_count, message, transfer = result["image_id"], result["new_count"], result["message"], result["transfer"]
                    is_new = new_count > 0
                    conn = get_connection()
                    try:
                        conn.execute("UPDATE import_item SET status = ?, image_id = ?, error = NULL WHERE batch_id = ? AND remote_id = ?", ("downloaded" if is_new else "skipped", image_id, batch_id, remote_id))
                        conn.commit()
                    finally:
                        conn.close()
                    job["progress"]["downloaded" if is_new else "skipped"] += new_count if is_new else 1
                except StaticPreviewUnavailable as exc:
                    conn = get_connection()
                    conn.execute("UPDATE import_item SET status = 'skipped', error = ? WHERE batch_id = ? AND remote_id = ?", (str(exc), batch_id, remote_id))
                    conn.commit()
                    conn.close()
                    job["progress"]["skipped"] += 1
                    transfer = None
                    message = f"Skipped {remote_id}: {exc}"
                except Exception as exc:
                    emit("imports.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
                    conn = get_connection()
                    conn.execute("UPDATE import_item SET status = 'error', error = ? WHERE batch_id = ? AND remote_id = ?", (str(exc), batch_id, remote_id))
                    conn.commit()
                    conn.close()
                    job["progress"]["errors"] += 1
                    transfer = None
                    message = f"{remote_id}: {exc}"
                finally:
                    active_files.pop(key, None)
                emit("import.item", message, remote_id=remote_id)
                job["progress"]["completed"] += 1
                job["progress"]["current_file"] = transfer
                job["progress"]["active_files"] = list(active_files.values())
                job["logs"] = [*job.get("logs", []), {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "message": message,
                    "current_file": transfer,
                }][-100:]

        await asyncio.gather(*(process_one(index, remote_id) for index, remote_id in enumerate(remote_ids, 1)))
        conn = get_connection()
        cancellation_requested = conn.execute("SELECT cancel_requested FROM import_batch WHERE id = ?", (batch_id,)).fetchone()
        conn.close()
        final_status = "canceled" if job["status"] == "canceled" or (cancellation_requested and cancellation_requested[0]) else "completed"
        completed_at = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        if final_status == "canceled":
            conn.execute("UPDATE import_item SET status = 'canceled' WHERE batch_id = ? AND status IN ('queued', 'running')", (batch_id,))
        conn.execute("UPDATE import_batch SET status = ?, completed_at = ? WHERE id = ?", (final_status, completed_at, batch_id))
        conn.commit()
        conn.close()
        job["status"] = final_status
        if final_status == "canceled":
            job["progress"]["current_file"] = None
            job["progress"]["active_files"] = []
        job["finished_at"] = completed_at
    except Exception as exc:
        emit("imports.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
        job.update(status="failed", error=str(exc), finished_at=datetime.now(timezone.utc).isoformat())
        conn = get_connection()
        conn.execute("UPDATE import_batch SET status = 'failed', error = ?, completed_at = ? WHERE id = ?", (str(exc), datetime.now(timezone.utc).isoformat(), batch_id))
        conn.commit()
        conn.close()
    finally:
        job_event(job, "import")
        if provider is not None:
            await provider.close()


@router.post("/batches", status_code=202)
async def create_import_batch(request: ImportBatchCreate):
    if not supported_provider(request.provider):
        raise HTTPException(status_code=400, detail=f"Provider '{request.provider}' is unavailable for imports")
    if request.provider == "gelbooru" and not all(get_gelbooru_credentials()):
        raise HTTPException(status_code=409, detail="Gelbooru requires a User ID and API key. Configure them in Settings before importing.")
    if request.provider in GALLERY_DL_PROVIDERS:
        ready, reason = provider_ready(request.provider)
        if not ready:
            raise HTTPException(status_code=409, detail=reason)
    conn = get_connection()
    if not conn.execute("SELECT 1 FROM collection WHERE id = ?", (request.folder_id,)).fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Folder not found")
    remote_ids = _unique_remote_ids(request.remote_ids)
    if not remote_ids:
        conn.close()
        raise HTTPException(status_code=422, detail="remote_ids must contain at least one non-empty ID")
    now = datetime.now(timezone.utc).isoformat()
    job_id = uuid.uuid4().hex
    conn.execute("INSERT INTO import_batch (collection_id, provider, status, created_at, job_id) VALUES (?, ?, 'queued', ?, ?)", (request.folder_id, request.provider, now, job_id))
    batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.executemany("INSERT INTO import_item (batch_id, remote_id) VALUES (?, ?)", [(batch_id, remote_id) for remote_id in remote_ids])
    conn.commit()
    conn.close()
    _jobs[job_id] = _new_job(job_id, batch_id, len(remote_ids))
    asyncio.create_task(_run_import(job_id, batch_id, request.folder_id, request.provider, remote_ids))
    return _jobs[job_id]


@router.get("/jobs/{job_id}")
async def get_import_job(job_id: str):
    if job_id in _jobs:
        return _jobs[job_id]
    conn = get_connection()
    batch = conn.execute("SELECT id, collection_id, provider, status, error, job_id FROM import_batch WHERE job_id = ?", (job_id,)).fetchone()
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="Import job not found")
    counts = {row[0]: row[1] for row in conn.execute("SELECT status, COUNT(*) FROM import_item WHERE batch_id = ? GROUP BY status", (batch[0],)).fetchall()}
    total = sum(counts.values())
    completed = sum(counts.get(status, 0) for status in {"downloaded", "skipped", "error", "canceled"})
    conn.close()
    return {
        "job_id": job_id,
        "batch_id": batch[0],
        "folder_id": batch[1],
        "provider": batch[2],
        "status": batch[3],
        "error": batch[4],
        "progress": {
            "total": total,
            "completed": completed,
            "downloaded": counts.get("downloaded", 0),
            "skipped": counts.get("skipped", 0),
            "errors": counts.get("error", 0),
        },
        "logs": [],
    }


@router.post("/batches/{batch_id}/start", status_code=202)
async def start_import_batch(batch_id: int):
    """Start a queued batch, or return its existing job when already started."""
    conn = get_connection()
    batch = conn.execute("SELECT id, collection_id, provider, status, job_id FROM import_batch WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch[4] and batch[4] in _jobs:
        conn.close()
        return _jobs[batch[4]]
    if batch[3] in {"completed", "failed", "canceled"}:
        result = dict(batch)
        conn.close()
        return result
    remote_ids = [row[0] for row in conn.execute("SELECT remote_id FROM import_item WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()]
    job_id = uuid.uuid4().hex
    conn.execute("UPDATE import_batch SET job_id = ?, status = 'queued', cancel_requested = 0 WHERE id = ?", (job_id, batch_id))
    conn.commit()
    conn.close()
    _jobs[job_id] = _new_job(job_id, batch_id, len(remote_ids))
    asyncio.create_task(_run_import(job_id, batch_id, batch[1], batch[2], remote_ids))
    return _jobs[job_id]


@router.get("/batches/{batch_id}")
async def get_import_batch(batch_id: int):
    conn = get_connection()
    batch = conn.execute("SELECT * FROM import_batch WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="Import batch not found")
    items = [dict(row) for row in conn.execute("SELECT * FROM import_item WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()]
    conn.close()
    result = dict(batch)
    result["items"] = items
    if result.get("job_id") in _jobs:
        result["job"] = _jobs[result["job_id"]]
    return result


@router.post("/batches/{batch_id}/cancel")
async def cancel_import_batch(batch_id: int):
    conn = get_connection()
    batch = conn.execute("SELECT status, job_id FROM import_batch WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        conn.close()
        raise HTTPException(status_code=404, detail="Import batch not found")
    if batch[0] in {"completed", "failed", "canceled"}:
        conn.close()
        return {"batch_id": batch_id, "status": batch[0]}
    conn.execute("UPDATE import_batch SET cancel_requested = 1 WHERE id = ?", (batch_id,))
    conn.commit()
    conn.close()
    if batch[1] in _jobs:
        _jobs[batch[1]]["cancel_requested"] = True
        if _jobs[batch[1]]["status"] in {"queued", "running"}:
            _jobs[batch[1]]["status"] = "cancelling"
    return {"batch_id": batch_id, "status": "cancel_requested"}
