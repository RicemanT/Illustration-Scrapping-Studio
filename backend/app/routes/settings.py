import asyncio
import signal
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.models import ParallelismSettings
from app.services.settings import MAX_PARALLEL_WORKERS, get_parallel_workers, set_parallel_workers


router = APIRouter()


@router.get("/parallelism")
async def read_parallelism():
    return {
        "workers": get_parallel_workers(),
        "minimum": 1,
        "maximum": MAX_PARALLEL_WORKERS,
        "recommended": 2,
    }


@router.put("/parallelism")
async def update_parallelism(settings: ParallelismSettings):
    workers = set_parallel_workers(settings.workers)
    from app.services.settings import configure_worker_pool
    configure_worker_pool(workers)
    from app.services.diagnostics import emit
    emit("settings.workers", f"Parallel worker count set to {workers}", workers=workers)
    return {
        "workers": workers,
        "minimum": 1,
        "maximum": MAX_PARALLEL_WORKERS,
        "recommended": 2,
    }


from app.models import ProcessingSettings
from app.services.settings import get_processing_settings, set_processing_settings

@router.get('/processing')
def read_processing():
    return get_processing_settings()

@router.put('/processing')
def update_processing(settings: ProcessingSettings):
    result = set_processing_settings(settings.model_dump())
    from app.services.diagnostics import emit
    emit("settings.processing", "Processing preferences saved for future jobs", processing=result)
    return result


from pydantic import BaseModel, Field

class StorageSettings(BaseModel):
    reserve_gib: float = Field(ge=0, le=1048576, allow_inf_nan=False)

@router.get('/server')
def read_server():
    from app.services.server import capabilities
    return capabilities()

@router.put('/storage')
def update_storage(settings: StorageSettings):
    from app.db import get_connection
    from datetime import datetime, timezone
    conn = get_connection()
    try:
        conn.execute("INSERT INTO app_setting(key,value,updated_at) VALUES('storage_reserve_gib',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (str(settings.reserve_gib), datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
    from app.services.diagnostics import emit
    emit('settings.storage', 'Free-space reserve updated', reserve_gib=settings.reserve_gib)
    return settings.model_dump()


class ShutdownRequest(BaseModel):
    confirm: Literal[True]


def _stop_server() -> None:
    # The same signal as the notebook's Stop cell and `kill -TERM`: uvicorn
    # finishes in-flight requests, the lifespan drains workers and leaves
    # interrupted jobs resumable, then the process exits.
    signal.raise_signal(signal.SIGTERM)


@router.post('/shutdown', status_code=202)
async def shutdown_server(request: ShutdownRequest):
    """Stop the whole app (backend and served UI) gracefully."""
    from app.services.diagnostics import emit
    emit("app.shutdown_requested", "Shutdown requested from Settings")
    # Let this response reach the browser first.
    asyncio.get_running_loop().call_later(0.5, _stop_server)
    return {"status": "stopping"}


class CaptionSettings(BaseModel):
    suffix: str


class MetaTagList(BaseModel):
    text: str


@router.get('/meta-tags')
def meta_tags_status():
    from app.services.meta_tags import status
    return status()


@router.get('/meta-tags/export')
def meta_tags_export():
    from fastapi.responses import PlainTextResponse
    from app.services.meta_tags import export_text
    return PlainTextResponse(export_text(), headers={'Content-Disposition': 'attachment; filename="meta-tags.txt"'})


@router.put('/meta-tags')
def meta_tags_save(request: MetaTagList):
    from fastapi import HTTPException
    from app.services.meta_tags import start
    try:
        return start(request.text)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ArtistTriggerSwitch(BaseModel):
    template: str
    group_id: int | None = None
    captions: bool = True


@router.get('/artist-trigger')
def artist_trigger_status():
    from app.services.artist_trigger import status
    return status()


@router.get('/artist-trigger/preview')
def artist_trigger_preview(template: str, group_id: int | None = None):
    from fastapi import HTTPException
    from app.services.artist_trigger import preview
    try:
        return preview(template, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post('/artist-trigger')
def artist_trigger_switch(request: ArtistTriggerSwitch):
    from fastapi import HTTPException
    from app.services.artist_trigger import start
    try:
        return start(request.template, request.group_id, request.captions)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class FlattenTransparent(BaseModel):
    folder_id: int | None = None   # one folder, or
    group_id: int | None = None    # one group, or neither: every folder


@router.get('/transparent')
def transparent_status():
    from app.services.transparency import status
    return status()


@router.post('/transparent')
def transparent_flatten(request: FlattenTransparent):
    from fastapi import HTTPException
    from app.services.transparency import start
    try:
        return start(request.folder_id, request.group_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ReferencePairsExport(BaseModel):
    group_ids: list[int] = []      # empty: every artist folder
    name: str = 'reference-pairs'


@router.get('/reference-pairs')
def reference_pairs_status():
    from app.services.reference_pairs import status
    return status()


@router.post('/reference-pairs')
def reference_pairs_export(request: ReferencePairsExport):
    import re
    from fastapi import HTTPException
    from app.services.reference_pairs import start
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', request.name):
        raise HTTPException(status_code=422, detail='Name: letters, digits, dot, dash and underscore only')
    try:
        return start(request.group_ids or None, None, request.name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get('/captions')
def read_caption_settings():
    from app.services.captions import DEFAULT_SUFFIX, get_suffix
    return {'suffix': get_suffix(), 'default': DEFAULT_SUFFIX}


@router.put('/captions')
def update_caption_settings(request: CaptionSettings):
    from fastapi import HTTPException
    from app.services.captions import DEFAULT_SUFFIX, set_suffix
    try:
        return {'suffix': set_suffix(request.suffix), 'default': DEFAULT_SUFFIX}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
