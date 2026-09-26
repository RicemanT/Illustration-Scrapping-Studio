from app.services.diagnostics import emit, context, job_event
import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.db import LIBRARY_PATH
from app.models import DedupScanRequest, DuplicateResolutionRequest
from app.services.dedup import DedupService


router = APIRouter()
dedup_service = DedupService(LIBRARY_PATH)
_jobs: dict[str, dict] = {}


async def _run_scan(job_id: str, request: DedupScanRequest) -> None:
    job = _jobs[job_id]
    context.set({**context.get(), "job_id": job_id, "folder_id": request.folder_id})
    emit("duplicates.started", "Duplicate scan started")
    job["status"] = "running"
    job["started_at"] = datetime.now(timezone.utc).isoformat()

    def update_progress(progress: dict) -> None:
        job["progress"] = progress

    try:
        result = await asyncio.to_thread(
            dedup_service.scan,
            request.folder_id,
            request.profile,
            update_progress,
        )
        job["result"] = result
        job["status"] = "completed"
    except Exception as exc:
        emit("duplicates.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
        job["error"] = str(exc)
        job["status"] = "failed"
    finally:
        job_event(job, "duplicates")
        job["completed_at"] = datetime.now(timezone.utc).isoformat()


@router.post("/scan")
async def scan_duplicates(request: DedupScanRequest):
    if request.profile not in {"strict", "balanced", "broad"}:
        raise HTTPException(status_code=422, detail="Profile must be strict, balanced, or broad")
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "folder_id": request.folder_id,
        "profile": request.profile,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
        "progress": {"phase": "queued", "completed": 0, "total": 1, "message": "Waiting to scan"},
        "result": None,
        "error": None,
    }
    asyncio.create_task(_run_scan(job_id, request))
    return _jobs[job_id]


@router.get("/jobs/{job_id}")
async def get_scan_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Duplicate scan job not found")
    return job


@router.get("")
async def list_duplicates(
    status: str = "pending",
    folder_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
):
    try:
        return dedup_service.list_candidates(status, folder_id, limit, offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{candidate_id}/resolve")
async def resolve_duplicate(candidate_id: int, request: DuplicateResolutionRequest):
    try:
        return dedup_service.resolve(candidate_id, request.action)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
