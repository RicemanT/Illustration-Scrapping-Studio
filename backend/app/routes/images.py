from fastapi import APIRouter, HTTPException
from app.db import LIBRARY_PATH
from app.services.images import ImageService
from app.models import ImageReviewUpdate
from app.services.media import media_decoder_status

router = APIRouter()

image_service = ImageService(LIBRARY_PATH)


@router.get("/media/decoder-status")
async def get_media_decoder_status():
    return media_decoder_status()


@router.get("/{image_id}")
async def get_image(image_id: int):
    """Get image details by ID."""
    image = image_service.get_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    return image


@router.delete("/{image_id}")
async def delete_image(image_id: int):
    if not image_service.delete_image(image_id):
        raise HTTPException(status_code=404, detail="Image not found")
    return {"status": "deleted", "image_id": image_id}


@router.patch("/{image_id}/review")
async def update_image_review(image_id: int, updates: ImageReviewUpdate):
    try:
        image = image_service.update_review(image_id, updates.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    return image
