from datetime import date
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from app.providers.booru import ProviderAuthenticationError
from app.providers.registry import create_provider, max_provider_limit

router = APIRouter()

@router.get("/{provider}")
async def search_provider(provider: str, query: str = Query(min_length=1), cursor: Optional[str] = None, limit: int = Query(default=50, ge=1, le=320), sort: str = "latest", date_from: Optional[str] = None, date_to: Optional[str] = None):
    if sort not in {"latest", "oldest"}:
        raise HTTPException(status_code=422, detail="sort must be latest or oldest")
    try:
        from_date = date.fromisoformat(date_from) if date_from else None
        to_date = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date filters must use YYYY-MM-DD") from exc
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="date_from must be before date_to")
    try:
        client = create_provider(provider)
    except ProviderAuthenticationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        page_size = min(limit, max_provider_limit(provider))
        posts, next_cursor = await client.search(query, cursor, page_size, sort=sort, date_from=date_from, date_to=date_to)
        return {"items": [post.model_dump() for post in posts], "next_cursor": next_cursor, "total": len(posts)}
    except ProviderAuthenticationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{provider} search failed: {exc}") from exc
    finally:
        await client.close()
