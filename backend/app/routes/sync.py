from app.services.diagnostics import emit, context, job_event
import asyncio
import time
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.db import LIBRARY_PATH, get_connection
from app.models import SyncScheduleUpdate
from app.providers.booru import BOORU_SITES, get_gelbooru_credentials
from app.providers.gallery_dl import GALLERY_DL_PROVIDERS, provider_ready
from app.providers.registry import create_provider, max_provider_limit, supported_provider
from app.services.sync import SyncService
from app.services.sync_jobs import (
    ACTIVE_STATUSES,
    advance_schedule,
    create_job,
    get_job as get_stored_job,
    get_schedule,
    list_jobs,
    save_job,
    unfinished_jobs,
    update_schedule,
)

router = APIRouter()
sync_service = SyncService(LIBRARY_PATH)
_jobs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}
_scheduler_task: asyncio.Task | None = None
_scheduler_wakeup: asyncio.Event | None = None
_shutting_down = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_job(kind: str, trigger: str, parameters: dict, folder_id: int | None = None, provider: str | None = None) -> dict:
    from app.services.settings import get_processing_settings
    parameters = {**parameters, "processing": get_processing_settings()}
    return {
        "job_id": uuid.uuid4().hex, "kind": kind, "trigger": trigger,
        "folder_id": folder_id, "provider": provider, "status": "queued",
        "parameters": parameters, "progress": {"phase": "queued", "total": 0, "completed": 0},
        "logs": [], "result": None, "error": None, "cancel_requested": False,
        "created_at": _now(), "started_at": None, "finished_at": None,
    }


def _persist(job: dict) -> None:
    save_job(job)


def _append_log(job: dict, update: dict) -> None:
    emit("sync.progress", update.get("message", "Sync progress"), "WARNING" if update.get("phase") in ("error", "retry") else "INFO", job_id=job["job_id"], progress=update)
    job["logs"] = [*job.get("logs", []), {"at": _now(), **update}][-100:]


def _spawn(job: dict) -> dict:
    _jobs[job["job_id"]] = job
    runner = _run_folder_job if job["kind"] == "folder" else _run_all_job
    task = asyncio.create_task(runner(job["job_id"]))
    _tasks[job["job_id"]] = task
    task.add_done_callback(lambda finished, job_id=job["job_id"]: _finalize_task(job_id, finished))
    return job


def _finalize_task(job_id: str, task: asyncio.Task) -> None:
    _tasks.pop(job_id, None)
    job = _jobs.get(job_id)
    if not job or not task.cancelled() or job.get("status") not in ACTIVE_STATUSES:
        return
    if _shutting_down and not job.get("cancel_requested"):
        job.update(status="queued", started_at=None, finished_at=None)
        job["progress"] = {**job.get("progress", {}), "phase": "queued", "message": "Interrupted by backend shutdown; will resume", "current_file": None, "active_files": []}
    else:
        job.update(status="canceled", finished_at=_now())
        job["progress"] = {**job.get("progress", {}), "phase": "canceled", "message": "Sync canceled", "current_file": None, "active_files": []}
    _persist(job)


async def _run_folder_job(job_id: str) -> None:
    job = _jobs[job_id]
    context.set({**context.get(), "job_id": job_id, "folder_id": job.get("folder_id"), "provider": job.get("provider")})
    params = job["parameters"]
    from app.services.settings import processing_context, get_processing_settings
    processing_context.set(params.get("processing") or get_processing_settings())
    job.update(status="running", started_at=job.get("started_at") or _now(), finished_at=None, error=None)
    last_persisted = 0.0

    def report(update: dict) -> None:
        nonlocal last_persisted
        update = dict(update)
        transient = update.pop("_transient", False)
        job["progress"] = {**job.get("progress", {}), **update}
        if not transient:
            _append_log(job, update)
        now = time.monotonic()
        if not transient or now - last_persisted >= 1.0:
            _persist(job)
            last_persisted = now

    _persist(job)
    try:
        result = await sync_service.sync_collection(
            int(job["folder_id"]), job["provider"], int(params.get("limit", 20)),
            progress=report, sort=params.get("sort", "latest"),
            date_from=params.get("date_from"), date_to=params.get("date_to"),
        )
        job.update(status="completed", result=result.model_dump(), finished_at=_now())
        job["progress"] = {**job.get("progress", {}), "phase": "completed", "current_file": None, "active_files": []}
    except asyncio.CancelledError:
        if _shutting_down and not job.get("cancel_requested"):
            job.update(status="queued", started_at=None, finished_at=None)
            job["progress"] = {**job.get("progress", {}), "phase": "queued", "message": "Interrupted by backend shutdown; will resume", "current_file": None, "active_files": []}
        else:
            job.update(status="canceled", finished_at=_now())
            job["progress"] = {**job.get("progress", {}), "phase": "canceled", "message": "Sync canceled", "current_file": None, "active_files": []}
        _persist(job)
        raise
    except Exception as exc:
        emit("sync.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
        _append_log(job, {"phase": "error", "message": str(exc)})
        job.update(status="failed", error=str(exc), finished_at=_now())
        job["progress"] = {**job.get("progress", {}), "phase": "failed", "message": str(exc), "current_file": None, "active_files": []}
    finally:
        _persist(job)


def _enabled_pairs() -> list[tuple[int, str, str]]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT c.id, c.name, cs.provider
        FROM collection c JOIN collection_source cs ON cs.collection_id = c.id
        WHERE c.enabled = 1 AND cs.enabled = 1 ORDER BY c.name, cs.provider
    """).fetchall()
    conn.close()
    return [(int(row[0]), row[1], row[2]) for row in rows]


async def _run_all_job(job_id: str) -> None:
    job = _jobs[job_id]
    context.set({**context.get(), "job_id": job_id, "folder_id": job.get("folder_id"), "provider": job.get("provider")})
    params = job["parameters"]
    from app.services.settings import processing_context, get_processing_settings
    processing_context.set(params.get("processing") or get_processing_settings())
    pairs = params.get('pairs') if 'group_id' in params else _enabled_pairs()
    pairs = pairs or []
    label = f"Group {params['group_name']}" if 'group_id' in params else 'Sync All'
    results = []
    aggregate = {"new_images": 0, "skipped": 0, "errors": 0}
    last_persisted = 0.0
    job.update(status="running", started_at=job.get("started_at") or _now(), finished_at=None, error=None)
    job["progress"] = {"phase": "sync_all", "total": len(pairs), "completed": 0, **aggregate, "message": f"Found {len(pairs)} enabled folder sources"}
    _persist(job)
    try:
        for pair_index, (folder_id, folder_name, provider) in enumerate(pairs, 1):
            context.set({**context.get(), "folder_id": folder_id, "provider": provider})
            if job.get("cancel_requested"):
                raise asyncio.CancelledError

            def report_pair(update: dict) -> None:
                nonlocal last_persisted
                transient = update.get("_transient", False)
                pair_progress = {key: value for key, value in update.items() if key != "_transient"}
                job["progress"] = {
                    **job.get("progress", {}), "phase": "sync_all", "total": len(pairs),
                    "completed": pair_index - 1, "current_folder": folder_name,
                    "current_folder_id": folder_id, "current_provider": provider,
                    "pair_progress": pair_progress, "current_file": pair_progress.get("current_file"),
                    "active_files": pair_progress.get("active_files", []), "workers": pair_progress.get("workers"),
                    "message": f"{folder_name} / {provider}: {pair_progress.get('message', 'syncing')}",
                }
                if not transient:
                    _append_log(job, {
                        "message": job["progress"]["message"],
                        "current_file": pair_progress.get("current_file"),
                    })
                now = time.monotonic()
                if not transient or now - last_persisted >= 1.0:
                    _persist(job)
                    last_persisted = now

            try:
                if 'group_id' in params:
                    conn = get_connection()
                    try:
                        eligible = conn.execute('''SELECT 1 FROM collection c JOIN collection_source cs ON cs.collection_id=c.id
                            WHERE c.id=? AND c.group_id=? AND c.enabled=1 AND cs.provider=? AND cs.enabled=1''',
                            (folder_id, params['group_id'], provider)).fetchone()
                    finally:
                        conn.close()
                    if not eligible:
                        raise ValueError('Folder was removed from this group or its source was disabled')
                result = await sync_service.sync_collection(
                    folder_id, provider, int(params.get("limit", 20)),
                    progress=report_pair, sort=params.get("sort", "latest"),
                    date_from=params.get("date_from"), date_to=params.get("date_to"),
                )
                dumped = result.model_dump()
            except Exception as exc:
                emit("sync.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
                dumped = {"folder_id": folder_id, "provider": provider, "new_images": 0, "skipped": 0, "errors": 1, "duration_seconds": 0, "error": str(exc)}
            results.append({"folder_name": folder_name, **dumped})
            for key in aggregate:
                aggregate[key] += dumped.get(key, 0)
            job["progress"] = {**job["progress"], "completed": pair_index, **aggregate, "message": f"Finished {folder_name} / {provider}", "current_file": None, "active_files": []}
            _append_log(job, {"message": job["progress"]["message"]})
            _persist(job)
        job.update(status="completed", result={**aggregate, "sources": results}, finished_at=_now())
        job["progress"] = {**job["progress"], "phase": "completed", "message": f"{label} completed", "current_file": None, "active_files": []}
    except asyncio.CancelledError:
        if _shutting_down and not job.get("cancel_requested"):
            job.update(status="queued", started_at=None, finished_at=None)
            job["progress"] = {**job.get("progress", {}), "phase": "queued", "message": "Interrupted by backend shutdown; will resume", "current_file": None, "active_files": []}
        else:
            job.update(status="canceled", finished_at=_now())
            job["progress"] = {**job.get("progress", {}), "phase": "canceled", "message": "Sync All canceled", "current_file": None, "active_files": []}
        _persist(job)
        raise
    except Exception as exc:
        emit("sync.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
        job.update(status="failed", error=str(exc), finished_at=_now())
        job["progress"] = {**job.get("progress", {}), "phase": "failed", "message": str(exc), "current_file": None, "active_files": []}
    finally:
        _persist(job)


def _start_new_job(job: dict) -> dict:
    create_job(job)
    return _spawn(job)


async def cancel_folder_syncs(folder_id: int) -> int:
    active = [(job_id, task) for job_id, task in list(_tasks.items())
              if (_jobs.get(job_id, {}).get("folder_id") == folder_id
                  or _jobs.get(job_id, {}).get('kind') == 'all') and not task.done()]
    for job_id, task in active:
        job = _jobs[job_id]
        context.set({**context.get(), "job_id": job_id, "folder_id": job.get("folder_id"), "provider": job.get("provider")})
        job.update(cancel_requested=True, status="cancelling")
        _persist(job)
        task.cancel()
    if active:
        await asyncio.gather(*(task for _, task in active), return_exceptions=True)
    return len(active)


async def resume_unfinished_jobs() -> int:
    resumed = 0
    for job in unfinished_jobs():
        if job.get("cancel_requested") or job.get("status") == "cancelling":
            job.update(status="canceled", finished_at=_now())
            save_job(job)
            continue
        job.update(status="queued", trigger="resumed", started_at=None, finished_at=None)
        job["progress"] = {**job.get("progress", {}), "phase": "queued", "message": "Resuming after backend restart", "current_file": None, "active_files": []}
        save_job(job)
        _spawn(job)
        resumed += 1
    return resumed


async def _scheduler_loop() -> None:
    global _scheduler_wakeup
    _scheduler_wakeup = asyncio.Event()
    while True:
        schedule = get_schedule()
        if schedule["enabled"]:
            next_run = datetime.fromisoformat(schedule["next_run_at"]) if schedule.get("next_run_at") else datetime.now(timezone.utc)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
            has_active_sync = any(job.get("status") in ACTIVE_STATUSES for job in _jobs.values())
            if datetime.now(timezone.utc) >= next_run and not has_active_sync:
                _start_new_job(_new_job("all", "scheduled", {"limit": schedule["limit_per_source"], "sort": schedule["sort"]}))
                advance_schedule()
        try:
            await asyncio.wait_for(_scheduler_wakeup.wait(), timeout=30)
            _scheduler_wakeup.clear()
        except asyncio.TimeoutError:
            pass


async def start_background_services() -> int:
    global _scheduler_task, _shutting_down
    _shutting_down = False
    resumed = await resume_unfinished_jobs()
    if not _scheduler_task or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())
    return resumed


async def stop_background_services() -> None:
    global _scheduler_task, _shutting_down
    _shutting_down = True
    tasks = [task for task in _tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        await asyncio.gather(_scheduler_task, return_exceptions=True)
    _scheduler_task = None


@router.get('/search/{provider}')
async def search_provider(provider: str, query: str, cursor: Optional[str] = None, limit: int = 50, sort: str = "latest", date_from: Optional[str] = None, date_to: Optional[str] = None):
    if not supported_provider(provider):
        raise HTTPException(status_code=400, detail=f'Unsupported provider: {provider}')
    try:
        client = create_provider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        page_size = min(max(limit, 1), max_provider_limit(provider))
        posts, next_cursor = await client.search(query, cursor, page_size, sort=sort, date_from=date_from, date_to=date_to)
        items = [post.model_dump() for post in posts]
        return {'items': items, 'posts': items, 'next_cursor': next_cursor, 'total': len(items), 'provider': provider, 'query': query}
    except Exception as exc:
        emit("sync.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@router.post("/folder/{folder_id}/{provider}", status_code=202)
async def sync_folder(folder_id: int, provider: str, limit: int = 20, sort: str = "latest", date_from: str | None = None, date_to: str | None = None):
    limit = min(max(limit, 1), 320)
    if not supported_provider(provider):
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
    if sort not in {"latest", "oldest"}:
        raise HTTPException(status_code=422, detail="sort must be latest or oldest")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    if provider == "gelbooru" and not all(get_gelbooru_credentials()):
        raise HTTPException(status_code=409, detail="Gelbooru requires a User ID and API key. Configure them in Settings before syncing.")
    if provider in GALLERY_DL_PROVIDERS:
        ready, reason = provider_ready(provider)
        if not ready:
            raise HTTPException(status_code=409, detail=reason)
    conn = get_connection()
    source = conn.execute(
        "SELECT enabled FROM collection_source WHERE collection_id = ? AND provider = ?",
        (folder_id, provider),
    ).fetchone()
    conn.close()
    if not source:
        raise HTTPException(status_code=404, detail="Folder source not found")
    if not source[0]:
        raise HTTPException(status_code=409, detail="This folder source is disabled")
    if any(job.get("kind") == "folder" and job.get("folder_id") == folder_id and job.get("provider") == provider and job.get("status") in ACTIVE_STATUSES for job in _jobs.values()):
        raise HTTPException(status_code=409, detail="This folder source already has an active sync")
    if any(job.get("kind") == "all" and job.get("status") in ACTIVE_STATUSES for job in _jobs.values()):
        raise HTTPException(status_code=409, detail="Sync All is currently running")
    return _start_new_job(_new_job("folder", "manual", {"limit": limit, "sort": sort, "date_from": date_from, "date_to": date_to}, folder_id, provider))


@router.post("/collection/{collection_id}/{provider}", status_code=202, include_in_schema=False)
async def sync_collection_legacy(collection_id: int, provider: str, limit: int = 20, sort: str = "latest", date_from: str | None = None, date_to: str | None = None):
    return await sync_folder(collection_id, provider, limit, sort, date_from, date_to)


@router.get("/jobs/history")
async def sync_history(limit: int = 50, group_id: int | None = None):
    # Stored history provides durability, while the in-memory copy contains
    # sub-second transfer telemetry for currently running jobs.  Overlay the
    # latter so Dashboard/folder observers see the same bytes, speed and ETA
    # as the client which originally started the job.
    stored = list_jobs(limit, group_id=group_id)
    items = [dict(_jobs.get(job["job_id"], job)) for job in stored]
    known = {job["job_id"] for job in items}
    live_only = [dict(job) for job_id, job in _jobs.items() if job_id not in known
                 and (group_id is None or job.get('parameters', {}).get('group_id') == group_id)]
    items = sorted([*live_only, *items], key=lambda job: job.get("created_at") or "", reverse=True)
    return {"items": items[:min(max(limit, 1), 200)]}


@router.get("/jobs/{job_id}")
async def get_sync_job(job_id: str):
    job = _jobs.get(job_id) or get_stored_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Sync job not found")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_sync_job(job_id: str):
    job = _jobs.get(job_id) or get_stored_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Sync job not found")
    if job.get("status") not in ACTIVE_STATUSES:
        return job
    job.update(cancel_requested=True, status="cancelling")
    job["progress"] = {**job.get("progress", {}), "phase": "cancelling", "message": "Stopping sync...", "current_file": None, "active_files": []}
    _jobs[job_id] = job
    _persist(job)
    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    else:
        job.update(status="canceled", finished_at=_now())
        _persist(job)
    return job


@router.post("/all", status_code=202)
async def sync_all(
    limit: int = 20,
    sort: str = "latest",
    date_from: str | None = None,
    date_to: str | None = None,
    trigger: str = "manual",
):
    limit = min(max(limit, 1), 320)
    if sort not in {"latest", "oldest"}:
        raise HTTPException(status_code=422, detail="sort must be latest or oldest")
    try:
        parsed_from = date.fromisoformat(date_from) if date_from else None
        parsed_to = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date filters must use YYYY-MM-DD") from exc
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    if any(job.get("status") in ACTIVE_STATUSES for job in _jobs.values()):
        raise HTTPException(status_code=409, detail="Another sync job is already running")
    return _start_new_job(_new_job(
        "all",
        "scheduled" if trigger == "scheduled" else "manual",
        {"limit": limit, "sort": sort, "date_from": date_from, "date_to": date_to},
    ))


@router.post('/group/{group_id}', status_code=202)
async def sync_group(group_id: int, limit: int = 20, sort: str = 'latest',
                     date_from: str | None = None, date_to: str | None = None):
    from app.services.groups import validate_group
    if sort not in {'latest', 'oldest'}:
        raise HTTPException(422, 'sort must be latest or oldest')
    try:
        first = date.fromisoformat(date_from) if date_from else None
        last = date.fromisoformat(date_to) if date_to else None
        if first and last and first > last:
            raise ValueError('date_from must be before date_to')
    except ValueError as exc:
        raise HTTPException(422, 'Use YYYY-MM-DD dates with From on or before To') from exc
    if any(job.get('status') in ACTIVE_STATUSES for job in _jobs.values()):
        raise HTTPException(409, 'Another sync job is already running')
    conn = get_connection()
    try:
        try:
            group = validate_group(conn, group_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        provider = group['provider']
        if provider == 'gelbooru' and not all(get_gelbooru_credentials()):
            raise HTTPException(409, 'Configure Gelbooru credentials in Settings first')
        if provider in GALLERY_DL_PROVIDERS:
            ready, reason = provider_ready(provider)
            if not ready:
                raise HTTPException(409, reason)
        pairs = [list(row) for row in conn.execute('''SELECT c.id,c.name,cs.provider
            FROM collection c JOIN collection_source cs ON cs.collection_id=c.id
            WHERE c.group_id=? AND c.enabled=1 AND cs.enabled=1 AND cs.provider=? ORDER BY c.name,c.id''',
            (group_id, provider))]
    finally:
        conn.close()
    if not pairs:
        raise HTTPException(409, 'This group has no enabled folders with its provider enabled')
    return _start_new_job(_new_job('all', 'manual', {
        'group_id': group_id, 'group_name': group['name'], 'provider': provider,
        'pairs': pairs, 'limit': min(max(limit, 1), 320), 'sort': sort,
        'date_from': date_from, 'date_to': date_to,
    }))


@router.get("/schedule")
async def read_sync_schedule():
    return get_schedule()


@router.put("/schedule")
async def write_sync_schedule(settings: SyncScheduleUpdate):
    schedule = update_schedule(settings.enabled, settings.interval_minutes, settings.limit_per_source, settings.sort)
    if _scheduler_wakeup:
        _scheduler_wakeup.set()
    return schedule


@router.post("/schedule/run-now", status_code=202)
async def run_schedule_now():
    schedule = get_schedule()
    return await sync_all(limit=schedule["limit_per_source"], sort=schedule["sort"], trigger="scheduled")
