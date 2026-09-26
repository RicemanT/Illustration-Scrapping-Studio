from fastapi import APIRouter, HTTPException

from app.db import LIBRARY_PATH
from app.models import DatasetExportCreate
from app.services.qa import DatasetQAService

router = APIRouter()


@router.get("/validate/{folder_id}")
def validate_collection(folder_id: int):
    try:
        return DatasetQAService(LIBRARY_PATH).validate_collection(folder_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/")
def create_export(payload: DatasetExportCreate):
    try:
        return DatasetQAService(LIBRARY_PATH).create_export(payload.folder_id, payload.mode)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}") from exc


@router.get("/")
async def list_exports(folder_id: int | None = None):
    if folder_id is None:
        raise HTTPException(status_code=422, detail="folder_id is required")
    return {"items": DatasetQAService.list_exports(folder_id)}


@router.get("/{export_id}")
async def get_export(export_id: str):
    result = DatasetQAService.get_export(export_id)
    if not result:
        raise HTTPException(status_code=404, detail="Export not found")
    return result
