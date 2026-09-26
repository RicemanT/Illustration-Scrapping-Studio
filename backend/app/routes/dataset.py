from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.db import LIBRARY_PATH, get_connection
from app.services.filters import LocalFilter, LocalQuery
from app.services import dataset_jobs as jobs, filter_review

router = APIRouter()


class RepairRequest(BaseModel):
    scan_id: str
    issue_ids: list[int] = Field(min_length=1,max_length=200)
    confirmed: Literal[True]


class ApplyRequest(BaseModel):
    preview_token: str = Field(max_length=200000)
    image_ids: list[int] = Field(min_length=1,max_length=200)
    status: Literal['rejected','archived']
    confirmed: Literal[True]


@router.post('/folders/{folder_id}/scan')
async def scan(folder_id: int, filters: LocalFilter):
    try:
        job = jobs.create(folder_id, filters)
        jobs.launch(job['job_id'], LIBRARY_PATH)
        return job
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.get('/jobs')
def history(folder_id: int):
    from app.services.sync_jobs import row_to_job
    conn = get_connection()
    try:
        return {'items':[row_to_job(r) for r in conn.execute('SELECT * FROM dataset_job WHERE collection_id=? ORDER BY created_at DESC LIMIT 20',(folder_id,))]}
    finally:
        conn.close()


@router.get('/jobs/{job_id}')
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404,'Dataset job not found')
    return job


@router.get('/jobs/{job_id}/issues')
def get_issues(job_id: str, offset: int = Query(0,ge=0), limit: int = Query(100,ge=1,le=200)):
    get_job(job_id)
    return jobs.issues(job_id,offset,limit)


@router.post('/jobs/{job_id}/cancel')
def cancel(job_id: str):
    get_job(job_id)
    return jobs.cancel(job_id)


@router.post('/jobs/{job_id}/resume')
async def resume(job_id: str):
    job = get_job(job_id)
    if job['status'] not in {'interrupted','failed','canceled'}:
        raise HTTPException(409,'Only interrupted, failed or canceled jobs can resume')
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM dataset_job WHERE status IN ('queued','running','cancelling')").fetchone():
            raise HTTPException(409,'Another dataset job is active')
        if not conn.execute('SELECT 1 FROM collection WHERE id=?',(job['folder_id'],)).fetchone():
            raise HTTPException(404,'Folder was deleted')
    finally:
        conn.close()
    job.update(status='queued',cancel_requested=False,finished_at=None)
    jobs.save(job)
    jobs.launch(job_id,LIBRARY_PATH)
    return job


@router.post('/folders/{folder_id}/repair')
async def repair(folder_id: int, request: RepairRequest):
    import json
    scan_job = get_job(request.scan_id)
    if scan_job['folder_id'] != folder_id or scan_job['kind'] != 'scan' or scan_job['status'] != 'completed':
        raise HTTPException(409,'Select issues from a completed scan of this folder')
    conn = get_connection()
    try:
        rows = conn.execute('SELECT payload FROM dataset_job_issue WHERE job_id=? AND id IN (SELECT value FROM json_each(?))', (request.scan_id,json.dumps(request.issue_ids))).fetchall()
        if len(rows) != len(set(request.issue_ids)):
            raise HTTPException(422,'Some selected issues do not belong to this scan')
        repairs = {}
        for row in rows:
            issue = json.loads(row[0])
            if not issue.get('allowed_actions') or not issue.get('image_id'):
                raise HTTPException(422,'Selected issue has no safe automatic repair')
            repairs.setdefault(str(issue['image_id']),set()).update(issue['allowed_actions'])
    finally:
        conn.close()
    try:
        job = jobs.create(folder_id,LocalFilter(image_ids=[int(i) for i in repairs]), {key:sorted(value) for key,value in repairs.items()})
        jobs.launch(job['job_id'],LIBRARY_PATH)
        return job
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.post('/folders/{folder_id}/preview')
def preview(folder_id: int, query: LocalQuery):
    try:
        return filter_review.preview(folder_id,query)
    except LookupError as exc:
        raise HTTPException(404,str(exc)) from exc


@router.post('/folders/{folder_id}/review')
def review(folder_id: int, request: ApplyRequest):
    try:
        return filter_review.apply(folder_id,request.preview_token,request.image_ids,request.status)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.post('/folders/{folder_id}/review/undo/{token}')
def undo(folder_id: int, token: str):
    try:
        return filter_review.undo(folder_id,token)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
