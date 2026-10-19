"""Switch the artist trigger format ("Drawn by {artist}" <-> "@{artist}") of many artist folders at once.

The trigger is always the first ground-truth tag (built from the folder's `artist_tag_template` and its name) and
the start of the natural-language caption ("Drawn by x. ..."). Switching sets the template of every artist folder in
scope, rewrites their tag sidecars, and in each caption replaces the leading trigger, in whichever form it is
written, with the new one ("@x. ..."); captions without one (MOSS-captioned ones) get it added. Accepted folders
are included: this only changes the trigger's spelling, never which images or tags a folder has. Running it again
with the other template switches back.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import app.db as db
from app.services.captions import caption_file, get_suffix
from app.services.tags import TagService, normalize_tag

KNOWN_TEMPLATES = ("Drawn by {artist}", "@{artist}")
# Any leading trigger sentence the captioners write, whatever artist spelling it uses.
LEADING_TRIGGER = re.compile(r"^\s*(?:(?:drawn|art|illustrated)\s+by\s+[^.\n]{1,120}|@[^.\n]{1,120})\.\s*", re.IGNORECASE)

_lock = threading.Lock()
_state: dict = {"status": "idle"}


def validate_template(template: str) -> str:
    value = (template or "").strip()
    if "{artist}" not in value:
        raise ValueError("The trigger format must contain {artist}, e.g. @{artist} or Drawn by {artist}")
    if any(character in value for character in (",", "\n", "\r", ".")):
        raise ValueError("The trigger format cannot contain commas, periods or line breaks")
    if len(value) > 200:
        raise ValueError("The trigger format is limited to 200 characters")
    return value


def render(template: str, folder_name: str) -> str:
    """The trigger tag of a folder, exactly as the ground-truth tags spell it (TagService._source_tags)."""
    return normalize_tag(template.replace("{artist}", normalize_tag(folder_name).casefold()))


def recaption(text: str, old_triggers: list[str], new_trigger: str) -> str:
    """The caption with its leading trigger (any of `old_triggers`, or any "Drawn by ..." / "@..." sentence)
    replaced by `new_trigger`, or with it added when there is none."""
    rest = text.strip()
    for trigger in sorted({t for t in old_triggers if t}, key=len, reverse=True):
        match = re.match(r"\s*" + re.escape(trigger) + r"\s*[.,:;]?\s*", rest, re.IGNORECASE)
        if match:
            rest = rest[match.end():]
            break
    else:
        match = LEADING_TRIGGER.match(rest)
        if match:
            rest = rest[match.end():]
    return new_trigger + "." + (" " + rest if rest else "")


def _folders(group_id: Optional[int]) -> list[dict]:
    conn = db.get_connection()
    try:
        sql = "SELECT id, name, artist_tag_template FROM collection WHERE type = 'artist'"
        params: list = []
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        return [dict(row) for row in conn.execute(sql + " ORDER BY name", params).fetchall()]
    finally:
        conn.close()


def _images(folder_id: int) -> list[tuple[int, str]]:
    conn = db.get_connection()
    try:
        return [(row[0], row[1]) for row in conn.execute(
            "SELECT id, path FROM image WHERE folder_id = ? AND path IS NOT NULL ORDER BY id", (folder_id,)).fetchall()]
    finally:
        conn.close()


def preview(template: str, group_id: Optional[int] = None) -> dict:
    template = validate_template(template)
    folders = _folders(group_id)
    conn = db.get_connection()
    try:
        images = 0
        for start in range(0, len(folders), 500):
            chunk = [f["id"] for f in folders[start:start + 500]]
            if chunk:
                images += conn.execute(f"SELECT COUNT(*) FROM image WHERE folder_id IN ({','.join('?' * len(chunk))})",
                                       chunk).fetchone()[0]
    finally:
        conn.close()
    example = None
    if folders:
        folder = folders[0]
        before = render(folder["artist_tag_template"], folder["name"]) if folder["artist_tag_template"] else None
        example = {"folder": folder["name"], "before": before, "after": render(template, folder["name"])}
    unchanged = sum(1 for f in folders if (f["artist_tag_template"] or "") == template)
    return {"template": template, "folders": len(folders), "images": images, "unchanged_folders": unchanged,
            "example": example}


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(text.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply(template: str, group_id: Optional[int] = None, captions: bool = True, progress=lambda **_: None) -> dict:
    template = validate_template(template)
    folders = _folders(group_id)
    library = db.LIBRARY_PATH
    images_root = library / "images"
    suffix = get_suffix()
    tags = TagService(library)
    stats = {"folders": len(folders), "sidecars": 0, "captions_changed": 0, "captions_added": 0, "captions_missing": 0}
    for done, folder in enumerate(folders, 1):
        new_trigger = render(template, folder["name"])
        old_triggers = [render(t, folder["name"]) for t in {folder["artist_tag_template"], *KNOWN_TEMPLATES} if t]
        conn = db.get_connection()
        try:
            conn.execute("UPDATE collection SET artist_tag_template = ?, updated_at = ? WHERE id = ?",
                         (template, datetime.now(timezone.utc).isoformat(), folder["id"]))
            conn.commit()
        finally:
            conn.close()
        images = _images(folder["id"])
        tags._rewrite_sidecars(image_id for image_id, _ in images)
        stats["sidecars"] += len(images)
        if captions:
            for _, relative in images:
                path = caption_file(images_root / relative, suffix)
                if not path.is_file():
                    stats["captions_missing"] += 1
                    continue
                text = path.read_text(encoding="utf-8")
                new_text = recaption(text, old_triggers, new_trigger)
                if new_text.strip() != text.strip():
                    had = any(re.match(r"\s*" + re.escape(t), text, re.IGNORECASE) for t in old_triggers) \
                        or bool(LEADING_TRIGGER.match(text))
                    stats["captions_changed" if had else "captions_added"] += 1
                    _write(path, new_text + ("\n" if text.endswith("\n") else ""))
        progress(done=done, total=len(folders))
    return stats


def status() -> dict:
    with _lock:
        return dict(_state)


def start(template: str, group_id: Optional[int] = None, captions: bool = True) -> dict:
    template = validate_template(template)
    with _lock:
        if _state.get("status") == "running":
            raise RuntimeError("A trigger switch is already running")
        _state.clear()
        _state.update(status="running", template=template, group_id=group_id, captions=captions, done=0, total=0,
                      started_at=datetime.now(timezone.utc).isoformat())

    def progress(done: int, total: int):
        with _lock:
            _state.update(done=done, total=total)

    def work():
        try:
            result = apply(template, group_id, captions, progress)
            with _lock:
                _state.update(status="done", result=result, finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:   # reported to the page; the folders done so far keep the new format
            with _lock:
                _state.update(status="error", error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=work, name="artist-trigger", daemon=True).start()
    return status()
