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
    """Add or update a source without deleting its history or pagination."""
    if "enabled" not in payload and "query_override" not in payload:
        raise HTTPException(422, "enabled or query_override is required")
    if "enabled" in payload and not isinstance(payload['enabled'], bool):
        raise HTTPException(422, "enabled must be a boolean")
    if 'query_override' in payload and payload['query_override'] is not None and not isinstance(payload['query_override'], str):
        raise HTTPException(422, 'query_override must be a string or null')
    from app.services.queries import validate_collection_query, assert_search_idle
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        folder = conn.execute('SELECT type,query FROM collection WHERE id=?', (folder_id,)).fetchone()
        if not folder:
            raise HTTPException(404, 'Folder not found')
        source = conn.execute('SELECT query_override FROM collection_source WHERE collection_id=? AND provider=?', (folder_id,provider)).fetchone()
        value = (payload.get('query_override') or '').strip() if 'query_override' in payload else (source[0] if source else None)
        validate_collection_query(value or folder['query'], folder['type'], provider)
        assert_search_idle(conn, folder_id)
        conn.execute('INSERT OR IGNORE INTO collection_source(collection_id,provider,enabled) VALUES(?,?,0)', (folder_id,provider))
        if 'enabled' in payload:
            conn.execute('UPDATE collection_source SET enabled=? WHERE collection_id=? AND provider=?', (int(payload['enabled']),folder_id,provider))
        if 'query_override' in payload and (value or None) != (source[0] if source else None):
            conn.execute('UPDATE collection_source SET query_override=?,last_cursor=NULL,backfill_cursor=NULL WHERE collection_id=? AND provider=?', (value or None,folder_id,provider))
        conn.commit()
        return {'folder_id':folder_id,'provider':provider,**payload}
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc
    finally:
        conn.close()


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
