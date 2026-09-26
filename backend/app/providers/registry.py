from typing import Any

from app.providers.base import Provider
from app.providers.booru import BOORU_SITES, BooruProvider, get_gelbooru_credentials
from app.providers.gallery_dl import (
    GALLERY_DL_PROVIDERS, GalleryDLProvider, gallery_dl_runtime, provider_max_limit,
    provider_ready,
)


def create_provider(name: str) -> Provider:
    if name in BOORU_SITES:
        return BooruProvider(name)
    if name in GALLERY_DL_PROVIDERS:
        return GalleryDLProvider(name)
    raise ValueError(f"Unknown provider '{name}'")


def supported_provider(name: str) -> bool:
    return name in BOORU_SITES or name in GALLERY_DL_PROVIDERS


def max_provider_limit(name: str) -> int:
    if name in BOORU_SITES:
        return int(BOORU_SITES[name].get("max_limit", 100))
    return provider_max_limit(name)


def provider_descriptors() -> list[dict[str, Any]]:
    gelbooru_configured = all(get_gelbooru_credentials())
    items = [{
        "name": name, "collection_types": ["artist", "character", "tag"], "type": "booru", "available": name != "gelbooru" or gelbooru_configured,
        "requires_auth": bool(config.get("requires_auth", False)),
        "capabilities": ["search", "lookup", "download", "pagination", "normalized_metadata"],
        "rate_limit_seconds": config.get("rate_limit", 0),
        "unavailable_reason": "Gelbooru User ID and API key are not configured" if name == "gelbooru" and not gelbooru_configured else None,
    } for name, config in BOORU_SITES.items()]
    runtime = gallery_dl_runtime()
    for name, config in GALLERY_DL_PROVIDERS.items():
        available, reason = provider_ready(name)
        items.append({
            "name": name, "collection_types": ["artist"], "type": "gallery-dl", "available": available,
            "requires_auth": bool(config.get("requires_auth")),
            "credentials_configured": available if config.get("requires_auth") else None,
            "capabilities": ["profile", "lookup", "download", "pagination", "normalized_metadata"] if runtime["installed"] else [],
            "rate_limit_seconds": None, "unavailable_reason": reason,
            "runtime_version": runtime["version"],
        })
    return items
