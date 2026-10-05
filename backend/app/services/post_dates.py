"""Post dates as the sites send them.

Danbooru and e621 send ISO 8601 timestamps; Gelbooru sends text such as
`Sat Mar 02 13:05:57 -0600 2024`. Harvested rows keep the raw value, so
anything that sorts or groups by date goes through these helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone

GELBOORU_FORMAT = '%a %b %d %H:%M:%S %z %Y'

# The same year rule in SQL, for aggregate queries over millions of rows.
YEAR_SQL = ("CASE WHEN substr(created_at,1,4) GLOB '[12][0-9][0-9][0-9]' THEN CAST(substr(created_at,1,4) AS INTEGER) "
            "WHEN substr(created_at,-4) GLOB '[12][0-9][0-9][0-9]' THEN CAST(substr(created_at,-4) AS INTEGER) ELSE 0 END")


def post_year(created_at) -> int:
    """The year a post was created, or 0 when unknown."""
    text = str(created_at or '').strip()
    for part in (text[:4], text[-4:]):
        if len(part) == 4 and part.isdigit() and part[0] in '12':
            return int(part)
    return 0


def sortable_time(created_at) -> str:
    """A UTC ISO string that sorts chronologically for every site's format ('' when unknown)."""
    text = str(created_at or '').strip()
    if not text:
        return ''
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.strptime(text, GELBOORU_FORMAT)
        except ValueError:
            return text if text[:4].isdigit() else ''
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
