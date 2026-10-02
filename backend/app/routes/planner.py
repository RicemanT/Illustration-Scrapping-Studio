"""Dataset Planner API: artist/character inputs, metadata harvest, plan runs and review."""
import asyncio
from typing import Literal, Optional

import httpx

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import LIBRARY_PATH
from app.services import planner_delivery as delivery
from app.services import planner_harvest as harvest
from app.services import planner_store as store
from app.services import planner_tags
from app.services.planner_select import PlannerConfig

router = APIRouter()


class CsvUpload(BaseModel):
    csv: str = Field(min_length=1, max_length=20 * 1024 * 1024)
    disable_missing: bool = True


class HarvestRequest(BaseModel):
    sites: list[Literal['danbooru', 'gelbooru', 'e621']] = Field(default_factory=lambda: ['danbooru', 'gelbooru', 'e621'])
    max_posts_per_artist: int = Field(2000, ge=20, le=100000)
    refresh: bool = False


class OverrideRequest(BaseModel):
    artist_id: int
    site: Literal['danbooru', 'gelbooru', 'e621']
    remote_id: str = Field(min_length=1, max_length=64)
    action: Literal['lock', 'ban', 'clear']


@router.get('/status')
def status():
    return store.status()


@router.post('/artists/import')
def import_artists(upload: CsvUpload):
    result = store.import_artists(upload.csv, upload.disable_missing)
    if result['errors']:
        raise HTTPException(422, result)
    return result


@router.post('/characters/import')
def import_characters(upload: CsvUpload):
    result = store.import_characters(upload.csv)
    if result['errors']:
        raise HTTPException(422, result)
    return result


class CharacterFetch(BaseModel):
    danbooru: int = Field(3000, ge=0, le=50000, description='Top Danbooru characters (also used for Gelbooru)')
    e621: int = Field(3000, ge=0, le=50000)


class SeriesRequest(BaseModel):
    series: list[str] = Field(min_length=1, max_length=50, description='Series (copyright) tags, e.g. blue_archive')
    families: list[Literal['danbooru', 'e621']] = Field(default_factory=lambda: ['danbooru', 'e621'])
    min_posts: int = Field(30, ge=1, le=100000, description='Skip characters with fewer posts on the site')


class TagCheck(BaseModel):
    family: Literal['danbooru', 'e621']
    tags: list[str] = Field(min_length=1, max_length=2000)


@router.post('/characters/fetch')
async def fetch_characters(request: CharacterFetch):
    """Replace character targets with each site's most-posted characters."""
    results = {}
    for family in ('danbooru', 'e621'):
        count = getattr(request, family)
        if not count:
            continue
        try:
            rows = await planner_tags.top_characters(family, count)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f'{family} tag lookup failed: {exc}') from exc
        results[family] = store.replace_family_characters(family, rows, 'top_by_post_count')['imported']
    return {'imported': results}


@router.post('/characters/series')
async def priority_series(request: SeriesRequest):
    """Add every character of the given series as priority targets."""
    results = {}
    for family in request.families:
        rows, per_series = [], {}
        for series in request.series:
            try:
                found = await planner_tags.series_characters(family, series)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            except httpx.HTTPError as exc:
                raise HTTPException(502, f'{family} tag lookup failed: {exc}') from exc
            kept = [row for row in found if row['post_count'] >= request.min_posts]
            per_series[series] = len(kept)
            rows.extend(kept)
        unique = list({row['tag']: row for row in rows}.values())
        results[family] = {**store.add_priority_characters(family, unique, 'series:' + ','.join(request.series)), 'series': per_series}
    return {'results': results}


@router.delete('/characters/priority')
def clear_priority():
    return {'removed': store.clear_priority_characters()}


@router.post('/tags/check')
async def check_tags(request: TagCheck):
    try:
        return {'family': request.family, 'items': await planner_tags.check_tags(request.family, request.tags)}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f'{request.family} tag lookup failed: {exc}') from exc


@router.get('/artists')
def artists(site: Optional[str] = None, q: str = '', run_id: Optional[int] = None,
            run_status: Optional[Literal['kept', 'dropped']] = None,
            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    return store.list_artists(site, q, run_id, run_status, offset, limit)


@router.get('/artists/{artist_id}')
def artist(artist_id: int, run_id: Optional[int] = None):
    try:
        return store.artist_detail(artist_id, run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/overrides')
def override(request: OverrideRequest):
    try:
        store.set_override(request.artist_id, request.site, request.remote_id, request.action)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {'ok': True}


@router.post('/harvest')
async def start_harvest(request: HarvestRequest):
    try:
        job = harvest.create_job(request.sites, request.max_posts_per_artist, request.refresh)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    harvest.launch(job['id'])
    return job


@router.post('/harvest/cancel')
def cancel_harvest():
    if not harvest.cancel():
        raise HTTPException(409, 'No harvest is running')
    return {'ok': True}


@router.get('/config/defaults')
def config_defaults():
    return PlannerConfig().model_dump()


@router.post('/runs')
def start_run(config: PlannerConfig):
    try:
        return store.get_run(store.start_plan(config))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/runs/{run_id}')
def get_run(run_id: int):
    try:
        return store.get_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post('/runs/{run_id}/export')
def export(run_id: int):
    try:
        target = store.export_manifest(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'path': str(target), 'files': sorted(p.name for p in target.iterdir())}


class DeliveryRequest(BaseModel):
    group_prefix: str = Field('Planner', min_length=1, max_length=80, description='Groups are named "<prefix> <site>"')


@router.post('/runs/{run_id}/deliver')
async def start_delivery(run_id: int, request: DeliveryRequest):
    if harvest._task and not harvest._task.done():
        raise HTTPException(409, 'Wait for the harvest to finish before downloading')
    try:
        job = delivery.create_delivery(run_id, request.group_prefix, LIBRARY_PATH)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    delivery.launch(job['id'])
    return job


@router.get('/deliveries/{delivery_id}')
def get_delivery(delivery_id: int):
    job = delivery.get_delivery(delivery_id)
    if not job:
        raise HTTPException(404, 'Delivery not found')
    return job


@router.post('/deliveries/{delivery_id}/resume')
async def resume_delivery(delivery_id: int):
    try:
        job = delivery.resume_delivery(delivery_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    delivery.launch(delivery_id)
    return job


@router.post('/deliveries/cancel')
def cancel_delivery():
    if not delivery.cancel():
        raise HTTPException(409, 'No delivery is running')
    return {'ok': True}


@router.post('/deliveries/{delivery_id}/layout')
def training_layout(delivery_id: int):
    try:
        target = delivery.export_training_layout(delivery_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {'path': str(target), 'files': sorted(p.name for p in target.iterdir())}


@router.get('/runs/{run_id}/manifest')
def manifest(run_id: int):
    path = store.planner_dir() / 'exports' / f'run-{run_id}' / 'manifest.jsonl'
    if not path.exists():
        raise HTTPException(404, 'Export this run first')
    return FileResponse(path, media_type='application/x-ndjson', filename=f'planner-run-{run_id}-manifest.jsonl')


_thumb_slots = asyncio.Semaphore(6)
_thumb_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    # One pooled client: building TLS contexts per request costs seconds on Windows.
    global _thumb_client
    if _thumb_client is None or _thumb_client.is_closed:
        _thumb_client = httpx.AsyncClient(timeout=20, follow_redirects=True,
                                          headers={'User-Agent': 'ArtistCollectionBuilder/1.0 (https://github.com/artist-collection)'})
    return _thumb_client


@router.get('/thumbs/{artist_id}/{site}/{remote_id}')
async def thumbnail(artist_id: int, site: Literal['danbooru', 'gelbooru', 'e621'], remote_id: str):
    """Serve a harvested post's preview through the backend.

    Danbooru's CDN rejects browser hotlinks without a challenge cookie. Only
    preview URLs already stored for harvested posts are fetched, and each one
    is cached under the planner folder.
    """
    if not remote_id.isdigit():
        raise HTTPException(404, 'Unknown post')
    path = store.planner_dir() / 'thumbs' / site / f'{remote_id}.jpg'
    if not path.exists():
        conn = store.connect()
        try:
            row = conn.execute('SELECT preview_url FROM post WHERE artist_id=? AND site=? AND remote_id=?', (artist_id, site, remote_id)).fetchone()
        finally:
            conn.close()
        if not row or not row['preview_url'] or not row['preview_url'].startswith('https://'):
            raise HTTPException(404, 'No preview for this post')
        headers = {'Referer': 'https://gelbooru.com/'} if site == 'gelbooru' else {}
        async with _thumb_slots:
            try:
                response = await _client().get(row['preview_url'], headers=headers)
            except httpx.HTTPError as exc:
                raise HTTPException(502, f'Preview fetch failed: {exc}') from exc
        if response.status_code != 200 or not response.headers.get('content-type', '').startswith('image/'):
            raise HTTPException(502, f'{site} returned HTTP {response.status_code} for the preview')
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_bytes(response.content)
        temp.replace(path)
    return FileResponse(path, headers={'Cache-Control': 'max-age=604800'})


async def close_thumbnail_client() -> None:
    if _thumb_client is not None:
        await _thumb_client.aclose()
