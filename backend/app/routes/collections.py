from fastapi import APIRouter, HTTPException
import sqlite3
from typing import List

from app.models import CollectionCreate, Collection, CollectionWithStats, CollectionImageBulkAction
from app.services.collections import CollectionService
from app.db import LIBRARY_PATH, get_connection
from app.services.images import ImageService
from app.services.filters import LocalQuery, list_images, explore_tags

router = APIRouter()
collection_service = CollectionService()


@router.post("/reconcile")
async def reconcile_library():
    """Re-index existing artist folders without deleting data."""
    return ImageService(LIBRARY_PATH).reconcile_filesystem()


@router.post("/", response_model=Collection)
async def create_collection(collection: CollectionCreate):
    """Create a new artist folder."""
    try:
        return collection_service.create_collection(collection)
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise HTTPException(status_code=503, detail="Database is busy; please retry in a moment") from e
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/", response_model=List[CollectionWithStats])
async def list_collections():
    """List all artist folders."""
    return collection_service.list_collections()


@router.get("/{folder_id}", response_model=CollectionWithStats)
async def get_collection(folder_id: int):
    """Get a folder by ID."""
    collection = collection_service.get_collection(folder_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Folder not found")
    return collection


@router.patch("/{folder_id}", response_model=Collection)
async def update_collection(folder_id: int, updates: dict):
    """Update a folder."""
    if 'group_id' in updates:
        raise HTTPException(422, 'Use the group move endpoint to move folders and their files together')
    try:
        collection = collection_service.update_collection(folder_id, updates)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not collection:
        raise HTTPException(status_code=404, detail="Folder not found")
    return collection


@router.patch("/{folder_id}/sources/{provider}")
async def update_collection_source(folder_id: int, provider: str, payload: dict):
    """Update one provider's enablement or provider-specific artist identity."""
    if "enabled" not in payload and "query_override" not in payload:
        raise HTTPException(status_code=422, detail="enabled or query_override is required")
    if "enabled" in payload and not isinstance(payload["enabled"], bool):
        raise HTTPException(status_code=422, detail="enabled must be a boolean")
    conn = get_connection()
    exists = conn.execute("SELECT type,query FROM collection WHERE id = ?", (folder_id,)).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(status_code=404, detail="Folder not found")
    source = conn.execute("SELECT 1 FROM collection_source WHERE collection_id = ? AND provider = ?", (folder_id, provider)).fetchone()
    if not source:
        conn.close()
        raise HTTPException(status_code=404, detail="Provider is not configured for this folder")
    from app.services.queries import validate_collection_query, assert_search_idle
    try:
        if "query_override" in payload:
            assert_search_idle(conn, folder_id)
        validate_collection_query(payload.get('query_override') or exists[1], exists[0], provider)
    except ValueError as exc:
        conn.close()
        raise HTTPException(422, str(exc)) from exc
    if "enabled" in payload:
        conn.execute("UPDATE collection_source SET enabled = ? WHERE collection_id = ? AND provider = ?", (1 if payload["enabled"] else 0, folder_id, provider))
    if "query_override" in payload:
        value = str(payload.get("query_override") or "").strip()
        if any(character in value for character in "\r\n\0"):
            conn.close()
            raise HTTPException(status_code=422, detail="Invalid source query")
        conn.execute("UPDATE collection_source SET query_override = ?, last_cursor = NULL, backfill_cursor = NULL WHERE collection_id = ? AND provider = ?", (value or None, folder_id, provider))
    conn.commit()
    conn.close()
    return {"folder_id": folder_id, "provider": provider, **payload}


@router.delete("/{folder_id}")
async def delete_collection(folder_id: int):
    """Delete a folder."""
    # Drain writers first so a canceled sync cannot recreate this folder's
    # isolated storage directory after deletion.
    from app.routes.sync import cancel_folder_syncs
    await cancel_folder_syncs(folder_id)
    deleted = collection_service.delete_collection(folder_id, LIBRARY_PATH)
    if not deleted:
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"status": "deleted"}


@router.get("/{folder_id}/images")
def get_collection_images(
    folder_id: int,
    limit: int = 100,
    offset: int = 0
):
    """Get images owned by a folder."""
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)
    try:
        return list_images(folder_id, LocalQuery(limit=limit, offset=offset))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{folder_id}/images/query")
def query_collection_images(folder_id: int, query: LocalQuery):
    try:
        return list_images(folder_id, query)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{folder_id}/tags/query")
def query_collection_tags(folder_id: int, query: LocalQuery, search: str = "", category: str | None = None):
    if not collection_service.get_collection(folder_id):
        raise HTTPException(404, "Folder not found")
    return explore_tags(folder_id, query, search[:256], category)


@router.post("/{folder_id}/images/bulk")
async def bulk_collection_images(folder_id: int, action: CollectionImageBulkAction):
    if action.action != "remove":
        raise HTTPException(status_code=422, detail="Only the reversible remove action is supported")
    import uuid
    token = uuid.uuid4().hex
    try:
        removed = collection_service.remove_images(folder_id, action.image_ids, LIBRARY_PATH, token)
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"removed": len(removed), "undo_token": token}


@router.post("/{folder_id}/images/bulk/undo/{token}")
async def undo_bulk_collection_images(folder_id: int, token: str):
    try:
        restored = collection_service.restore_images(folder_id, token, LIBRARY_PATH)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"restored": restored}


@router.get("/{folder_id}/images/bulk/recovery/latest")
async def latest_bulk_collection_image_recovery(folder_id: int):
    return {"recovery": collection_service.latest_image_recovery(folder_id, LIBRARY_PATH)}
