"""Small, local-only store for provider credentials and preferences."""

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


from app.db import LIBRARY_PATH


PROVIDER_SETTINGS_PATH = Path(os.getenv(
    "ARTIST_PROVIDER_SETTINGS_PATH", str(LIBRARY_PATH / "provider_settings.json")
)).expanduser().resolve()
_settings_lock = Lock()


def read_provider_settings() -> dict[str, Any]:
    try:
        value = json.loads(PROVIDER_SETTINGS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def update_provider_settings(provider: str, values: dict[str, Any]) -> None:
    """Atomically replace one provider's settings without exposing them."""
    with _settings_lock:
        settings = read_provider_settings()
        settings[provider] = values
        PROVIDER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROVIDER_SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        temporary.replace(PROVIDER_SETTINGS_PATH)
