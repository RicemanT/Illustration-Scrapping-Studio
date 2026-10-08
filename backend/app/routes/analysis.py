"""Image analysis: environment, settings, worker jobs, review flags and calibration."""
import asyncio
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.analysis import review, service
from app.analysis.store import AnalysisConfig, failure_report, get_config, save_config, stats

router = APIRouter()
_environment: dict = {'checked': 0.0, 'info': None}


class JobRequest(BaseModel):
    scope: Literal['planner', 'enabled', 'artists'] = 'planner'
    artist_ids: Optional[list[int]] = Field(None, max_length=100000)
    reanalyze: bool = False


class TokenRequest(BaseModel):
    token: str = Field('', max_length=400)


class ResetRequest(BaseModel):
    confirm: Literal[True]
    artist_ids: Optional[list[int]] = Field(None, max_length=100000)


async def _environment_info(refresh: bool = False) -> dict:
    if refresh or _environment['info'] is None or time.monotonic() - _environment['checked'] > 300:
        _environment['info'] = await asyncio.to_thread(service.environment)
        _environment['checked'] = time.monotonic()
    return _environment['info']


@router.get('/status')
async def status(refresh: bool = False):
    return {'environment': await _environment_info(refresh), 'install': service.install_status(), 'selftest': service.self_test_status(),
            'job': await asyncio.to_thread(service.job), 'stats': await asyncio.to_thread(stats), 'config': get_config().model_dump(),
            'gpus': await asyncio.to_thread(service.gpu_status), 'hf_token_set': bool(service.hf_token())}


@router.get('/failures')
async def failures(limit: int = 100):
    """Why posts could not be analysed (Start analysis queues failed posts again)."""
    return await asyncio.to_thread(failure_report, max(1, min(limit, 1000)))


@router.get('/gpus')
async def gpus():
    return {'gpus': await asyncio.to_thread(service.gpu_status)}


@router.put('/config')
def update_config(config: AnalysisConfig):
    return save_config(config).model_dump()


@router.put('/token')
def update_token(request: TokenRequest):
    service.save_hf_token(request.token)
    return {'hf_token_set': bool(service.hf_token())}


@router.post('/install')
def install():
    try:
        result = service.install()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    _environment['info'] = None
    return result


@router.post('/selftest')
def selftest():
    try:
        return service.self_test()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/jobs')
async def start_job(request: JobRequest):
    try:
        return await asyncio.to_thread(service.start_job, request.scope, request.artist_ids, request.reanalyze)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/jobs/stop')
def stop_job():
    return service.stop_job() or {'status': 'idle'}


@router.post('/jobs/{job_id}/resume')
def resume_job(job_id: int):
    try:
        return service.resume_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/samples/clear')
def clear_samples():
    try:
        return service.clear_samples()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get('/folders/{folder_id}/review')
async def folder_review(folder_id: int):
    result = await asyncio.to_thread(review.folder_review, folder_id)
    if result is None:
        raise HTTPException(404, 'This collection was not created by the Dataset Planner')
    return result


@router.get('/calibration')
async def calibration(source: Literal['auto', 'backup', 'live'] = 'auto'):
    return await asyncio.to_thread(review.calibration, source)


@router.post('/reset-curation')
async def reset_curation(request: ResetRequest):
    """Back up hand curation and clear it for a fresh, analysis-driven plan (the redo)."""
    return await asyncio.to_thread(review.reset_curation, request.artist_ids)
