"""Metadata tags worth training on: an allow-list.

Booru "meta" tags are mostly noise for training (commentary, translation request, commission), so the meta section
is off in the ground truth by default. A few describe the image and are worth keeping: the medium
(traditional media, watercolor (medium)), the software (procreate, clip studio paint), artifacts (jpeg artifacts).
The allow-list puts exactly those into every image's ground truth when the image has them, while the meta section
stays off. `export_text` lists every distinct meta tag of the library with its image count, to edit into the list:
names containing "commentary" or "request" come pre-commented, as almost never useful.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import app.db as db
from app.services.tags import GROUND_TRUTH_TAG_PROVIDERS, TagService, normalize_tag

SETTING = "meta_tag_allowlist"
AUTO_SKIP = ("commentary", "request")

_lock = threading.Lock()
_state: dict = {"status": "idle"}


def allowlist(conn=None) -> list[str]:
    own = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute("SELECT value FROM app_setting WHERE key = ?", (SETTING,)).fetchone()
    finally:
        if own:
            conn.close()
    try:
        return list(json.loads(row[0])) if row else []
    except (TypeError, ValueError):
        return []


def parse(text: str) -> list[str]:
    """One tag per line; anything after # is a comment; underscores or spaces, any case."""
    tags, seen = [], set()
    for line in (text or "").splitlines():
        tag = normalize_tag(line.split("#", 1)[0])
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            tags.append(tag)
    return tags


def library_meta_tags() -> list[dict]:
    """Every distinct meta tag on the library's images (from the booru sources the ground truth trusts)."""
    trusted = sorted(GROUND_TRUTH_TAG_PROVIDERS)
    conn = db.get_connection()
    try:
        rows = conn.execute(
            f"""SELECT it.tag, it.image_id, src.provider FROM image_tag it
                JOIN image_source src ON src.id = it.source_id
                JOIN image i ON i.id = it.image_id
                WHERE it.category = 'meta' AND i.folder_id IS NOT NULL
                  AND src.provider IN ({','.join('?' * len(trusted))})""", trusted).fetchall()
    finally:
        conn.close()
    tags: dict[str, dict] = {}
    for tag, image_id, provider in rows:
        name = normalize_tag(tag)
        entry = tags.setdefault(name.casefold(), {"tag": name, "images": set(), "sites": set()})
        entry["images"].add(image_id)
        entry["sites"].add(provider)
    return sorted(({"tag": e["tag"], "images": len(e["images"]), "sites": sorted(e["sites"])} for e in tags.values()),
                  key=lambda e: (-e["images"], e["tag"]))


def export_text() -> str:
    tags = library_meta_tags()
    allowed = {tag.casefold() for tag in allowlist()}
    def line(entry, commented):
        return ("# " if commented else "") + entry["tag"] + "    # " + f"{entry['images']:,} images · " + ", ".join(entry["sites"])
    keep = [e for e in tags if not any(word in e["tag"].casefold() for word in AUTO_SKIP)]
    skip = [e for e in tags if e not in keep]
    header = [
        "# Metadata tags of this library, most common first: the number of images having each, and their sites.",
        "# Lines without a leading # are the allow-list: those tags join every image's ground truth when it has them.",
        "# Delete (or comment out with #) the tags you do not want, then paste or load this file in Settings.",
        "# The current allow-list is uncommented; with none yet, every tag below is." if allowed else
        "# No allow-list yet: every tag below is uncommented, ready to be trimmed.",
        "",
    ]
    body = [line(e, bool(allowed) and e["tag"].casefold() not in allowed) for e in keep]
    tail = ["", '# Names with "commentary" or "request" (almost never useful), commented out:'] + \
           [line(e, e["tag"].casefold() not in allowed) for e in skip] if skip else []
    return "\n".join(header + body + tail) + "\n"


def _save(tags: list[str]) -> None:
    conn = db.get_connection()
    try:
        conn.execute(
            """INSERT INTO app_setting (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (SETTING, json.dumps(tags), datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()


def apply(tags: list[str], progress=lambda **_: None) -> dict:
    """Save the allow-list and rewrite every folder image's sidecar."""
    _save(tags)
    conn = db.get_connection()
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM image WHERE folder_id IS NOT NULL ORDER BY id").fetchall()]
    finally:
        conn.close()
    service = TagService(db.LIBRARY_PATH)
    warnings = 0
    for start in range(0, len(ids), 500):
        warnings += len(service._rewrite_sidecars(ids[start:start + 500]))
        progress(done=min(start + 500, len(ids)), total=len(ids))
    return {"tags": len(tags), "sidecars": len(ids), "warnings": warnings}


def status() -> dict:
    with _lock:
        state = dict(_state)
    state["allowlist"] = allowlist()
    return state


def start(text: str) -> dict:
    tags = parse(text)
    with _lock:
        if _state.get("status") == "running":
            raise RuntimeError("The sidecars are still being rewritten for the previous list")
        _state.clear()
        _state.update(status="running", done=0, total=0, started_at=datetime.now(timezone.utc).isoformat())

    def progress(done: int, total: int):
        with _lock:
            _state.update(done=done, total=total)

    def work():
        try:
            result = apply(tags, progress)
            with _lock:
                _state.update(status="done", result=result, finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            with _lock:
                _state.update(status="error", error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=work, name="meta-tags", daemon=True).start()
    return status()
