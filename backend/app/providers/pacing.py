"""Request pacing shared by every job that talks to a booru site.

Each site has two independent paces: API requests (searches, post and tag
lookups, which the sites rate-limit) and file downloads (served from the
sites' file servers; tools such as gallery-dl do not throttle these at all).
Pacers are process-wide, so a sync and a planner download running at the same
time share one budget per site.

Documented limits (checked 2026-10-05):
  Danbooru  10 API requests/s hard cap per IP, regardless of account; "for
            longer sessions, stay at around 1 request per second".
  e621      2 API requests/s hard cap (exceeding it returns HTTP 503); aim for
            1/s sustained.
  Gelbooru  no published number; it throttles and asks for an API key.

When a site answers 429 (or 503 from e621), that pacer backs off: it honours
Retry-After, doubles its interval, and returns to normal over the next
successful requests.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.services.provider_settings import read_provider_settings

DEFAULTS = {
    'danbooru': {'api': 1.0, 'download': 0.2},
    'gelbooru': {'api': 1.0, 'download': 0.2},
    'e621': {'api': 1.0, 'download': 0.2},
}
# Fastest allowed interval per site: just inside each documented hard cap.
FLOORS = {
    'danbooru': {'api': 0.15, 'download': 0.0},
    'gelbooru': {'api': 0.25, 'download': 0.0},
    'e621': {'api': 0.55, 'download': 0.0},
}
CEILING = 30.0
MAX_BACKOFF = 8.0
_SETTINGS_KEY = 'pacing'
_settings_cache: tuple[float, dict] = (0.0, {})


def _saved() -> dict:
    """Saved overrides, re-read at most once a second."""
    global _settings_cache
    loaded_at, values = _settings_cache
    if time.monotonic() - loaded_at > 1.0:
        values = read_provider_settings().get(_SETTINGS_KEY, {}) or {}
        _settings_cache = (time.monotonic(), values)
    return values


def clamp(site: str, kind: str, value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULTS[site][kind]
    return min(max(number, FLOORS[site][kind]), CEILING)


def interval(site: str, kind: str) -> float:
    saved = _saved().get(site, {})
    return clamp(site, kind, saved.get(kind, DEFAULTS[site][kind]))


def settings() -> dict:
    return {site: {kind: interval(site, kind) for kind in ('api', 'download')} for site in DEFAULTS}


def save(values: dict) -> dict:
    from app.services.provider_settings import update_provider_settings
    global _settings_cache
    cleaned = {site: {kind: clamp(site, kind, (values.get(site) or {}).get(kind, DEFAULTS[site][kind]))
                      for kind in ('api', 'download')} for site in DEFAULTS}
    update_provider_settings(_SETTINGS_KEY, cleaned)
    _settings_cache = (0.0, {})
    return settings()


class Pacer:
    """Reserves request start times. Reservation is synchronous, so concurrent
    coroutines never share a slot and no asyncio lock (bound to one event
    loop) is needed."""

    def __init__(self):
        self.next_at = 0.0
        self.backoff = 1.0

    async def wait(self, base_interval: float) -> None:
        now = time.monotonic()
        start = max(now, self.next_at)
        self.next_at = start + base_interval * self.backoff
        if start > now:
            await asyncio.sleep(start - now)

    def succeeded(self) -> None:
        if self.backoff > 1.0:
            self.backoff = max(1.0, self.backoff * 0.9)

    def throttled(self, retry_after: Optional[float], base_interval: float) -> float:
        """Slow down after a rate-limit answer; returns the wait applied."""
        self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        wait = retry_after if retry_after is not None else max(base_interval * self.backoff, 2.0)
        self.next_at = max(self.next_at, time.monotonic() + wait)
        return wait


_pacers: dict[tuple[str, str], Pacer] = {}


def pacer(site: str, kind: str) -> Pacer:
    return _pacers.setdefault((site, kind), Pacer())


def retry_after_seconds(response) -> Optional[float]:
    value = response.headers.get('retry-after') if response is not None else None
    try:
        return min(float(value), 120.0) if value is not None else None
    except ValueError:
        return None


def is_throttle(site: str, status_code: int) -> bool:
    return status_code == 429 or (site == 'e621' and status_code == 503)
