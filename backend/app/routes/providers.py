from fastapi import APIRouter, File, HTTPException, UploadFile
from app.providers.booru import BOORU_SITES, ProviderAuthenticationError, get_gelbooru_credentials, save_gelbooru_credentials
from app.providers.gallery_dl import (
    GALLERY_DL_PROVIDERS, TWITTER_BROWSERS, gallery_dl_runtime, get_gallery_dl_settings,
    provider_ready, remove_twitter_cookie_file, save_pixiv_refresh_token,
    save_twitter_browser, save_twitter_cookie_file,
)
from app.providers.registry import create_provider, provider_descriptors, supported_provider

router = APIRouter()


@router.get("")
async def list_providers():
    """Return provider capabilities without making remote requests."""
    return {"items": provider_descriptors()}


@router.get("/{provider}/status")
async def provider_status(provider: str):
    """Probe a provider's API and report a structured status."""
    if provider in GALLERY_DL_PROVIDERS:
        available, reason = provider_ready(provider)
        runtime = gallery_dl_runtime()
        return {
            "provider": provider, "available": available,
            "status": "configured" if available else "setup_required",
            "requires_auth": bool(GALLERY_DL_PROVIDERS[provider].get("requires_auth")),
            "credentials_configured": available if GALLERY_DL_PROVIDERS[provider].get("requires_auth") else None,
            "runtime_version": runtime["version"], "error": reason,
        }
    if not supported_provider(provider):
        return {"provider": provider, "available": False, "status": "unsupported", "error": "Unsupported provider"}
    client = create_provider(provider)
    if provider == "gelbooru" and not client.credentials_configured:
        await client.close()
        return {
            "provider": provider, "available": False, "status": "auth_required",
            "requires_auth": True, "credentials_configured": False,
            "error": "Gelbooru User ID and API key are not configured",
        }
    try:
        await client.search("", limit=1)
        return {"provider": provider, "available": True, "status": "ok", "requires_auth": bool(BOORU_SITES[provider].get("requires_auth", False)), "credentials_configured": True}
    except ProviderAuthenticationError as exc:
        return {"provider": provider, "available": False, "status": "auth_error", "error": str(exc), "requires_auth": True, "credentials_configured": True}
    except Exception as exc:
        return {"provider": provider, "available": False, "status": "error", "error": str(exc), "requires_auth": bool(BOORU_SITES[provider].get("requires_auth", False))}
    finally:
        await client.close()


@router.get("/gelbooru/config")
async def gelbooru_config():
    user_id, api_key = get_gelbooru_credentials()
    return {"user_id": user_id, "api_key_configured": bool(api_key), "configured": bool(user_id and api_key)}


@router.put("/gelbooru/config")
async def update_gelbooru_config(payload: dict):
    user_id = str(payload.get("user_id", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    if not user_id or not api_key:
        raise HTTPException(status_code=422, detail="Gelbooru User ID and API key are both required")
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail="Gelbooru User ID must be numeric")
    save_gelbooru_credentials(user_id, api_key)
    return {"user_id": user_id, "api_key_configured": True, "configured": True}


@router.get("/gallery-dl/config")
async def gallery_dl_config():
    settings = get_gallery_dl_settings()
    return {
        **gallery_dl_runtime(),
        "pixiv": {"configured": bool(settings["pixiv_refresh_token"])},
        "twitter": {
            "configured": (
                settings["twitter_auth_mode"] == "cookies_file" and bool(settings["twitter_cookie_file"])
            ) or (
                settings["twitter_auth_mode"] == "browser" and bool(settings["twitter_browser"])
            ),
            "auth_mode": settings["twitter_auth_mode"],
            "cookie_file_configured": bool(settings["twitter_cookie_file"]),
            "browser": settings["twitter_browser"], "profile": settings["twitter_profile"],
        },
        "twitter_browsers": sorted(TWITTER_BROWSERS),
    }


@router.put("/pixiv/config")
async def update_pixiv_config(payload: dict):
    refresh_token = str(payload.get("refresh_token", "")).strip()
    if not refresh_token:
        raise HTTPException(status_code=422, detail="Pixiv refresh token is required")
    save_pixiv_refresh_token(refresh_token)
    return {"configured": True}


@router.put("/twitter/config")
async def update_twitter_config(payload: dict):
    browser = str(payload.get("browser", "")).strip().lower()
    profile = str(payload.get("profile", "")).strip()
    if browser not in TWITTER_BROWSERS:
        raise HTTPException(status_code=422, detail="Choose a supported browser")
    if any(character in profile for character in "\r\n\0"):
        raise HTTPException(status_code=422, detail="Invalid browser profile")
    save_twitter_browser(browser, profile)
    return {"configured": True, "auth_mode": "browser", "browser": browser, "profile": profile}


@router.post("/twitter/cookies")
async def upload_twitter_cookies(file: UploadFile = File(...)):
    """Store a small Netscape cookie export on the backend that performs syncs."""
    contents = await file.read(2 * 1024 * 1024 + 1)
    await file.close()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="cookies.txt must be 2 MB or smaller")
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="cookies.txt must be UTF-8 text") from exc
    try:
        return save_twitter_cookie_file(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/twitter/cookies")
async def delete_twitter_cookies():
    return remove_twitter_cookie_file()
