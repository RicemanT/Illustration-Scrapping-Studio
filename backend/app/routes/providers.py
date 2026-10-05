from typing import Literal
from pydantic import BaseModel, Field
from app.services.provider_settings import update_provider_settings, read_provider_settings
from fastapi import APIRouter, File, HTTPException, UploadFile
from app.providers import pacing
from app.providers.booru import BOORU_SITES, ProviderAuthenticationError, get_account, get_gelbooru_credentials, save_account, save_gelbooru_credentials, user_agent
from app.providers.gallery_dl import (
    GALLERY_DL_PROVIDERS, TWITTER_BROWSERS, gallery_dl_runtime, get_gallery_dl_settings,
    provider_ready, remove_twitter_cookie_file, save_pixiv_refresh_token,
    save_twitter_browser, save_twitter_cookie_file, save_deviantart_cookie_file, remove_deviantart_cookie_file,
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


class PacingUpdate(BaseModel):
    danbooru: dict = Field(default_factory=dict)
    gelbooru: dict = Field(default_factory=dict)
    e621: dict = Field(default_factory=dict)


@router.get("/pacing")
async def get_pacing():
    """Seconds between request starts per booru site, with defaults and fastest allowed values."""
    return {"settings": pacing.settings(), "defaults": pacing.DEFAULTS, "floors": pacing.FLOORS}


@router.put("/pacing")
async def update_pacing(payload: PacingUpdate):
    return {"settings": pacing.save(payload.model_dump()), "defaults": pacing.DEFAULTS, "floors": pacing.FLOORS}


class AccountUpdate(BaseModel):
    login: str = Field(min_length=1, max_length=100)
    api_key: str = Field(min_length=1, max_length=200)


@router.get("/{site}/account")
async def account_status(site: Literal["danbooru", "e621"]):
    account = get_account(site)
    return {"login": account["login"], "user_id": account["user_id"],
            "configured": bool(account["login"] and account["api_key"])}


@router.put("/{site}/account")
async def update_account(site: Literal["danbooru", "e621"], payload: AccountUpdate):
    """Check the username and API key with the site, then save them."""
    import httpx
    login, api_key = payload.login.strip(), payload.api_key.strip()
    url = "https://danbooru.donmai.us/profile.json" if site == "danbooru" else "https://e621.net/posts.json?limit=1"
    headers = {"User-Agent": user_agent(site, {"login": login})}
    await pacing.pacer(site, "api").wait(pacing.interval(site, "api"))
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers, auth=httpx.BasicAuth(login, api_key)) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach {site}: {exc}") from exc
    if response.status_code in {401, 403}:
        raise HTTPException(status_code=422, detail=f"{site} rejected this username and API key. Copy the API key from your {site} profile page.")
    if response.is_error:
        raise HTTPException(status_code=502, detail=f"{site} returned HTTP {response.status_code} while checking the account")
    user_id = None
    if site == "danbooru":
        profile = response.json()
        if not isinstance(profile, dict) or not profile.get("id") or str(profile.get("name", "")).casefold() != login.casefold():
            raise HTTPException(status_code=422, detail="Danbooru did not accept this username and API key.")
        user_id = int(profile["id"])
    save_account(site, login, api_key, user_id)
    return {"login": login, "user_id": user_id, "configured": True}


@router.delete("/{site}/account")
async def remove_account(site: Literal["danbooru", "e621"]):
    update_provider_settings(site, {})
    return {"configured": False}


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
        "deviantart": {"cookie_file_configured": bool(settings["deviantart_cookie_file"]), "media_mode": settings["deviantart_media_mode"], "configured": bool(settings["deviantart"].get("refresh_token")), "custom_client_configured": bool(settings["deviantart"].get("client_id"))},
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


class DeviantArtSettings(BaseModel):
    refresh_token: str = Field(default='', max_length=4096)
    client_id: str = Field(default='', max_length=256)
    client_secret: str = Field(default='', max_length=4096)


@router.put('/deviantart/config')
async def update_deviantart_config(payload: DeviantArtSettings):
    values = {key: value.strip() for key, value in payload.model_dump().items()}
    if any(any(ord(c) < 32 for c in value) for value in values.values()):
        raise HTTPException(422, 'Credentials cannot contain control characters')
    if bool(values['client_id']) != bool(values['client_secret']):
        raise HTTPException(422, 'Custom client ID and secret must be supplied together')
    values['media_mode'] = (read_provider_settings().get('deviantart') or {}).get('media_mode', 'original')
    update_provider_settings('deviantart', values)
    return {'configured': bool(values['refresh_token']), 'custom_client_configured': bool(values['client_id'])}


class DeviantArtMedia(BaseModel):
    mode: Literal['original', 'published', 'prefer_original', 'published_preview']


@router.put('/deviantart/media')
async def update_deviantart_media(payload: DeviantArtMedia):
    values = dict(read_provider_settings().get('deviantart') or {})
    values['media_mode'] = payload.mode
    update_provider_settings('deviantart', values)
    return {'media_mode': payload.mode}


@router.post('/deviantart/cookies')
async def upload_deviantart_cookies(file: UploadFile = File(...)):
    contents = await file.read(2 * 1024 * 1024 + 1)
    await file.close()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(413, 'cookies.txt must be 2 MB or smaller')
    try:
        return save_deviantart_cookie_file(contents.decode('utf-8-sig'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(422, str(exc) if not isinstance(exc, UnicodeDecodeError) else 'cookies.txt must be UTF-8 text') from exc


@router.delete('/deviantart/cookies')
async def delete_deviantart_cookies():
    return remove_deviantart_cookie_file()
