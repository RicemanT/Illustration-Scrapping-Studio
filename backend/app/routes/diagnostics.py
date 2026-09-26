"""Read-only local diagnostics API; no request bodies or credentials are recorded."""
from fastapi import APIRouter, Query
from app.services.diagnostics import read_events
router = APIRouter()

@router.get("")
def history(level: str = Query('', pattern='^(|ACTIVITY|DEBUG|INFO|WARNING|ERROR)$'), search: str = Query('', max_length=200), request_id: str = Query('', max_length=80), job_id: str = Query('', max_length=80), folder_id: int | None = None, provider: str = Query('', max_length=80), before: str = Query('', max_length=120), limit: int = Query(100, ge=1, le=200)):
    return read_events(level=level, search=search, request_id=request_id, job_id=job_id, folder_id=folder_id, provider=provider, before=before, limit=limit)

from pydantic import BaseModel, Field
from typing import Literal
from app.services.diagnostics import emit

class ClientEvent(BaseModel):
    kind: Literal['action', 'navigation', 'error']
    message: str = Field(max_length=1000)
    path: str = Field(max_length=300)

@router.post('/client', status_code=202)
def client_event(event: ClientEvent):
    emit('ui.' + event.kind, event.message, 'ERROR' if event.kind == 'error' else 'INFO', ui_path=event.path.split('?')[0])
    return {'accepted': True}
