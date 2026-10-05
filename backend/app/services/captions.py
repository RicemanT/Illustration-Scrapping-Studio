"""Natural-language caption files beside each image.

Captioning tools write `<image stem><suffix>` (default `_nl.txt`) next to the
image and its ground-truth `.txt` sidecar. The app shows and edits these files
in place and keeps them with their image when it removes, recovers, merges,
deletes or exports images; it never generates them. Every save or delete first
copies the previous version to `<library>/.trash/captions/`.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

import app.db as db

DEFAULT_SUFFIX = '_nl.txt'
SETTING_KEY = 'caption_suffix'
_SUFFIX = re.compile(r'^(?:[._][A-Za-z0-9_-]{1,38})?\.[A-Za-z0-9]{1,12}$')
MAX_CAPTION_BYTES = 1024 * 1024


class CaptionConflict(Exception):
    """The file changed on disk since the editor loaded it."""


def get_suffix(conn=None) -> str:
    own = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute('SELECT value FROM app_setting WHERE key=?', (SETTING_KEY,)).fetchone()
    finally:
        if own:
            conn.close()
    return row[0] if row and row[0] else DEFAULT_SUFFIX


def validate_suffix(value: str) -> str:
    suffix = str(value or '').strip()
    if not _SUFFIX.fullmatch(suffix):
        raise ValueError('Use a file-name ending such as _nl.txt or .caption: an optional "_name" part, then an extension')
    if suffix.casefold() == '.txt':
        raise ValueError('.txt is the ground-truth tag sidecar; choose a different caption ending such as _nl.txt')
    return suffix


def set_suffix(value: str) -> str:
    suffix = validate_suffix(value)
    conn = db.get_connection()
    try:
        conn.execute("""INSERT INTO app_setting (key, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                     (SETTING_KEY, suffix, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally:
        conn.close()
    return suffix


def caption_relative(relative: str, suffix: str) -> str:
    """Library-relative caption path for a library-relative image path."""
    path = PurePosixPath(str(relative).replace('\\', '/'))
    return str(path.with_name(path.stem + suffix))


def caption_file(image_path: Path, suffix: str) -> Path:
    return image_path.with_name(image_path.stem + suffix)


def _version(path: Path) -> Optional[str]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return f'{stat.st_mtime_ns}-{stat.st_size}'


def _locate(image_id: int) -> tuple[Path, str]:
    conn = db.get_connection()
    try:
        row = conn.execute('SELECT path FROM image WHERE id=?', (image_id,)).fetchone()
        suffix = get_suffix(conn)
    finally:
        conn.close()
    if not row or not row[0]:
        raise LookupError('Image not found')
    root = (db.LIBRARY_PATH / 'images').resolve()
    path = (root / caption_relative(row[0], suffix)).resolve()
    if root not in path.parents:
        raise ValueError('Unsafe caption path')
    return path, suffix


def read_caption(image_id: int) -> dict:
    path, suffix = _locate(image_id)
    result = {'image_id': image_id, 'filename': path.name, 'suffix': suffix, 'exists': path.is_file(),
              'text': None, 'version': _version(path), 'error': None}
    if result['exists']:
        try:
            if path.stat().st_size > MAX_CAPTION_BYTES:
                result['error'] = 'The caption file is larger than 1 MiB and is not shown'
            else:
                result['text'] = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError) as exc:
            result['error'] = f'The caption file is not readable UTF-8 text: {exc}'
    return result


def _backup(path: Path) -> None:
    root = (db.LIBRARY_PATH / 'images').resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = db.LIBRARY_PATH / '.trash' / 'captions' / path.resolve().relative_to(root).parent / f'{path.name}.{stamp}'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)


def _check(path: Path, base_version: Optional[str]) -> None:
    if _version(path) != base_version:
        raise CaptionConflict('The caption file changed on disk since it was opened (for example, a captioning run '
                              'rewrote it). Reload it, then edit again.')


def write_caption(image_id: int, text: str, base_version: Optional[str]) -> dict:
    """Write the caption atomically, refusing if the file changed since `base_version`."""
    data = str(text).replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
    if len(data) > MAX_CAPTION_BYTES:
        raise ValueError('Captions are limited to 1 MiB')
    path, _ = _locate(image_id)
    _check(path, base_version)
    if path.exists():
        _backup(path)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return read_caption(image_id)


def delete_caption(image_id: int, base_version: Optional[str]) -> dict:
    path, _ = _locate(image_id)
    _check(path, base_version)
    if path.exists():
        _backup(path)
        path.unlink()
    return read_caption(image_id)


def caption_ids(folder_id: int, present: bool) -> list[int]:
    """Images of a folder that have (or lack) a caption file."""
    conn = db.get_connection()
    try:
        rows = conn.execute('SELECT id, path FROM image WHERE folder_id=? AND path IS NOT NULL', (folder_id,)).fetchall()
        suffix = get_suffix(conn)
    finally:
        conn.close()
    root = db.LIBRARY_PATH / 'images'
    return [row[0] for row in rows if (root / caption_relative(row[1], suffix)).is_file() == present]
