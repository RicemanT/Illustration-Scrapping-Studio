from fastapi import APIRouter, HTTPException
from typing import List
from app.db import LIBRARY_PATH, get_connection
from app.models import GroundTruthTagBatchReplace, GroundTruthTagsBulkAction, GroundTruthTagsReplace, TagCategoryPolicyUpdate
from app.services.tags import TagService

router = APIRouter()
tag_service = TagService(LIBRARY_PATH)


@router.get("/category-policy")
async def get_global_category_policy():
    return tag_service.get_global_category_policy()


@router.put("/category-policy")
async def update_global_category_policy(request: TagCategoryPolicyUpdate):
    if request.categories is None:
        raise HTTPException(status_code=422, detail="Global categories cannot inherit from another policy")
    try:
        return tag_service.set_global_category_policy(request.categories)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/folder/{folder_id}/category-policy")
async def get_folder_category_policy(folder_id: int):
    try:
        return tag_service.get_folder_category_policy(folder_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/folder/{folder_id}/category-policy")
async def update_folder_category_policy(folder_id: int, request: TagCategoryPolicyUpdate):
    try:
        return tag_service.set_folder_category_policy(folder_id, request.categories)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/search")
async def search_tags(query: str, limit: int = 50):
    """Search for tags across all images."""
    conn = get_connection()
    cursor = conn.cursor()

    normalized_query = query.replace("_", " ")
    cursor.execute("""
        SELECT category, tag, COUNT(*) as count
        FROM image_tag
        WHERE REPLACE(tag, '_', ' ') LIKE ?
        GROUP BY category, tag
        ORDER BY count DESC
        LIMIT ?
    """, (f"%{normalized_query}%", limit))

    results = [
        {"category": row[0], "tag": row[1].replace("_", " "), "count": row[2]}
        for row in cursor.fetchall()
    ]

    conn.close()
    return {"tags": results}


@router.get("/collection/{collection_id}", include_in_schema=False)
async def get_collection_tags(collection_id: int):
    """Get effective ground-truth tag counts for a collection."""
    try:
        return {"tags": tag_service.collection_ground_truth_tags(collection_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/folder/{folder_id}")
async def get_folder_tags(folder_id: int):
    return await get_collection_tags(folder_id)


@router.get("/image/{image_id}/ground-truth")
async def get_image_ground_truth_tags(image_id: int):
    try:
        return tag_service.get_ground_truth(image_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/image/{image_id}/ground-truth")
async def replace_image_ground_truth_tags(image_id: int, request: GroundTruthTagsReplace):
    try:
        return tag_service.replace_ground_truth(image_id, request.tags)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/collection/{collection_id}/bulk", include_in_schema=False)
async def bulk_edit_ground_truth_tags(collection_id: int, request: GroundTruthTagsBulkAction):
    try:
        return tag_service.bulk_edit(collection_id, request.image_ids, request.tags, request.action)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/folder/{folder_id}/bulk")
async def bulk_edit_folder_ground_truth_tags(folder_id: int, request: GroundTruthTagsBulkAction):
    return await bulk_edit_ground_truth_tags(folder_id, request)


@router.post("/collection/{collection_id}/bulk-replace", include_in_schema=False)
async def bulk_replace_ground_truth_tag(collection_id: int, request: GroundTruthTagBatchReplace):
    try:
        return tag_service.bulk_replace(
            collection_id, request.image_ids, request.old_tag, request.new_tag
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/folder/{folder_id}/bulk-replace")
async def bulk_replace_folder_ground_truth_tag(folder_id: int, request: GroundTruthTagBatchReplace):
    return await bulk_replace_ground_truth_tag(folder_id, request)


@router.post("/undo/{token}")
async def undo_ground_truth_tag_edit(token: str):
    try:
        return tag_service.undo(token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
