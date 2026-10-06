"""Dataset Tracker: character and general-tag coverage across all planner collections."""
import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services import tracker
from app.services.planner_store import now

router = APIRouter()

_job: dict = {'status': 'idle'}
_task: asyncio.Task | None = None


class GoalRequest(BaseModel):
    kind: Literal['character', 'general']
    family: Literal['danbooru', 'e621']
    tags: Optional[list[str]] = Field(None, max_length=5000)
    series: Optional[str] = Field(None, max_length=200)
    goal: Optional[int] = Field(None, ge=0, le=1_000_000)


class SnapshotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ImpactRequest(BaseModel):
    image_ids: list[int] = Field(min_length=1, max_length=5000)


async def _run_refresh() -> None:
    try:
        _job['result'] = await asyncio.to_thread(tracker.refresh_all, lambda **values: _job.update(values))
        _job.update(status='completed', finished_at=now())
    except Exception as exc:
        _job.update(status='failed', error=str(exc), finished_at=now())


@router.get('/status')
def status():
    return {'meta': tracker.meta(), 'job': _job}


@router.post('/refresh')
async def refresh():
    """Recount every collection, general tags and character series (background)."""
    global _task
    if _task and not _task.done():
        raise HTTPException(409, 'The tracker is already refreshing')
    _job.clear()
    _job.update(status='running', done=0, total=0, started_at=now(), error=None)
    _task = asyncio.get_running_loop().create_task(_run_refresh())
    return _job


def _table(kind, family, search, status, series, priority_only, targets_only, boost_only, present_only, sort, snapshot_id, offset, limit):
    return tracker.table(kind, family, search, status, series, priority_only, targets_only, boost_only, present_only, sort, snapshot_id, offset, limit)


@router.get('/characters')
def characters(family: Optional[Literal['danbooru', 'e621']] = None, search: str = '', status: Optional[Literal['missing', 'lost', 'below', 'met']] = None,
               series: str = '', priority_only: bool = False, targets_only: bool = False, present_only: bool = False,
               sort: Literal['gap', 'now', 'lost', 'spare', 'rank', 'name', 'delta'] = 'gap', snapshot_id: Optional[int] = None,
               offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return _table('character', family, search, status, series, priority_only, targets_only, False, present_only, sort, snapshot_id, offset, limit)


@router.get('/general')
def general(family: Optional[Literal['danbooru', 'e621']] = None, search: str = '', status: Optional[Literal['missing', 'lost', 'below', 'met']] = None,
            boost_only: bool = False, present_only: bool = False,
            sort: Literal['gap', 'now', 'lost', 'spare', 'name', 'delta'] = 'now', snapshot_id: Optional[int] = None,
            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return _table('general', family, search, status, '', False, False, boost_only, present_only, sort, snapshot_id, offset, limit)


@router.get('/characters/{family}/{tag}/folders')
def character_folders(family: Literal['danbooru', 'e621'], tag: str):
    return {'items': tracker.character_folders(family, tag)}


@router.put('/goals')
def set_goals(request: GoalRequest):
    if request.series:
        tags = tracker.series_characters(request.family, request.series)
    else:
        tags = [t.strip().replace(' ', '_') for t in request.tags or [] if t.strip()]
    if not tags:
        raise HTTPException(422, 'Choose at least one tag or a series')
    return {'updated': tracker.set_goal(request.kind, request.family, tags, request.goal)}


@router.get('/folders')
def folders():
    return tracker.folders()


@router.get('/folders/{folder_id}')
async def folder(folder_id: int):
    detail = await asyncio.to_thread(tracker.folder_detail, folder_id)
    if detail is None:
        raise HTTPException(404, 'This collection was not created by the Dataset Planner')
    # Dataset-wide totals need every collection counted once; start that in the background.
    counting = bool(_task and not _task.done())
    if tracker.meta() is None and not counting:
        await refresh()
        counting = True
    return {**detail, 'counting': counting}


@router.post('/folders/{folder_id}/impact')
def impact(folder_id: int, request: ImpactRequest):
    return tracker.deletion_impact(folder_id, request.image_ids)


@router.get('/snapshots')
def snapshots():
    return {'items': tracker.snapshots()}


@router.post('/snapshots')
def create_snapshot(request: SnapshotRequest):
    try:
        return tracker.create_snapshot(request.name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete('/snapshots/{snapshot_id}')
def delete_snapshot(snapshot_id: int):
    if not tracker.delete_snapshot(snapshot_id):
        raise HTTPException(404, 'Snapshot not found')
    return {'deleted': True}


def _csv(text: str, filename: str) -> Response:
    return Response(text, media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('/export.csv')
def export(kind: Literal['character', 'general'] = 'character', snapshot_id: Optional[int] = None):
    """The current table as CSV; `snapshot_id` adds the change since that snapshot."""
    return _csv(tracker.export_csv(kind, snapshot_id), f'tracker-{kind}s.csv')


@router.get('/snapshots/{snapshot_id}/export.csv')
def export_snapshot(snapshot_id: int):
    try:
        name, text = tracker.export_snapshot_csv(snapshot_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    safe = ''.join(ch if ch.isalnum() or ch in '.-_' else '-' for ch in name)
    return _csv(text, f'tracker-snapshot-{safe}.csv')
