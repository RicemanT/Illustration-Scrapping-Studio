"""Flatten transparent images onto solid backgrounds, for training.

Sprite-style art often has a transparent background. Trainers (and this app's thumbnails) that simply drop the
alpha channel show whatever colour data sits under the transparent pixels: noise, stripes or leftover layers, which
the model would learn as the background. This replaces each transparent image with the same picture composited onto
a solid colour from PALETTE, chosen evenly by the image's original hash (stable across runs, and no bias towards
white or black), and updates its tags: "transparent background" is removed, "simple background" and
"<colour> background" are added, all as ordinary tag edits.

The file keeps its path, so captions, annotations and masks stay paired with it. Its hash, size and fingerprints are
updated and the thumbnail is rebuilt. Its md5 keeps the original download's, so the image still counts as that post.
The original goes to `.trash/transparent-originals/` in the library. Animated images and videos are left alone.
Captions written before flattening may still mention a transparent background: the result counts those images so
they can be captioned again.
"""
from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image

import app.db as db
from app.services.storage_lock import pair_write

# Solid backgrounds with a matching booru tag ("<name> background"), all equally likely.
PALETTE = (
    ('white', (255, 255, 255)), ('black', (0, 0, 0)), ('grey', (128, 128, 128)), ('red', (200, 40, 40)),
    ('blue', (50, 90, 200)), ('green', (60, 160, 70)), ('yellow', (240, 210, 60)), ('pink', (240, 160, 190)),
    ('purple', (130, 80, 180)), ('orange', (240, 140, 40)), ('brown', (120, 80, 45)), ('aqua', (60, 200, 200)),
)
REMOVED_TAG = 'transparent background'
SKIP_FORMATS = {'gif', 'mp4', 'webm', 'mkv', 'mov', 'm4v'}

_lock = threading.Lock()
_state: dict = {'status': 'idle'}


def background_for(sha256: str) -> tuple[str, tuple[int, int, int]]:
    """The palette colour for an image, from its original hash: the same image always gets the same colour."""
    return PALETTE[int(sha256[:8], 16) % len(PALETTE)]


def has_transparency(image: Image.Image) -> bool:
    """Whether any pixel is less than fully opaque."""
    if image.mode == 'P':
        if 'transparency' not in image.info:
            return False
        image = image.convert('RGBA')
    if image.mode == 'PA':
        image = image.convert('RGBA')
    if 'A' not in image.getbands():
        return False
    low, _ = image.getchannel('A').getextrema()
    return low < 255


def flatten(image: Image.Image, colour: tuple[int, int, int]) -> Image.Image:
    """The image composited onto a solid colour, as RGB."""
    rgba = image.convert('RGBA')
    background = Image.new('RGBA', rgba.size, colour + (255,))
    background.alpha_composite(rgba)
    return background.convert('RGB')


def _folders(folder_id: Optional[int], group_id: Optional[int]) -> list[dict]:
    conn = db.get_connection()
    try:
        if folder_id is not None:
            rows = conn.execute('SELECT id, name, slug FROM collection WHERE id = ?', (folder_id,)).fetchall()
            if not rows:
                raise LookupError('Folder not found')
        elif group_id is not None:
            rows = conn.execute('SELECT id, name, slug FROM collection WHERE group_id = ? ORDER BY name', (group_id,)).fetchall()
        else:
            rows = conn.execute('SELECT id, name, slug FROM collection ORDER BY name').fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _save(image: Image.Image, path: Path, fmt: str) -> None:
    """Write next to `path`, then replace it, in the file's own format (lossless where that format allows)."""
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        if fmt in ('jpg', 'jpeg'):
            image.save(temporary, 'JPEG', quality=100, subsampling=0)
        elif fmt == 'webp':
            image.save(temporary, 'WEBP', lossless=True, quality=100, method=4)
        else:
            image.save(temporary, fmt.upper() if fmt in ('bmp', 'tiff') else 'PNG', **({} if fmt in ('bmp', 'tiff') else {'optimize': True}))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@pair_write
def flatten_image(image_id: int, library: Path) -> Optional[dict]:
    """Flatten one image if it is transparent. Returns what was done, or None when there was nothing to do."""
    from app.services.dedup import analyze_training_image
    from app.services.qa import _sha256
    conn = db.get_connection()
    try:
        row = conn.execute('SELECT * FROM image WHERE id = ?', (image_id,)).fetchone()
        if not row or not row['path']:
            return None
        images_root = (library / 'images').resolve()
        source = (images_root / row['path']).resolve()
        if images_root not in source.parents or not source.is_file():
            return None
        fmt = (row['format'] or source.suffix.lstrip('.')).lower()
        if fmt in SKIP_FORMATS:
            return None
        with Image.open(source) as raw:
            if getattr(raw, 'n_frames', 1) > 1:
                return None
            raw.load()
            if not has_transparency(raw):
                return None
            name, colour = background_for(row['sha256'])
            flat = flatten(raw, colour)
        if _sha256(source) != row['sha256']:
            raise ValueError(f'image {image_id}: file changed on disk; run a dataset scan first')
        backup = library / '.trash' / 'transparent-originals' / row['path']
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_bytes(source.read_bytes())
        _save(flat, source, fmt)
        sha256 = _sha256(source)
        slug = Path(row['path']).parts[0] if len(Path(row['path']).parts) > 1 else ''
        thumb_relative = f'{slug}/{sha256}_thumb.jpg' if slug else f'{sha256}_thumb.jpg'
        fingerprint, width, height, _ = analyze_training_image(source, library / 'thumbnails' / thumb_relative)
        old_thumb = row['thumb_path']
        conn.execute('UPDATE image SET sha256 = ?, file_size = ?, width = ?, height = ?, thumb_path = ?, phash = ?, dhash = ?, '
                     'gradient_hash = ?, colorhash = ? WHERE id = ?',
                     (sha256, source.stat().st_size, width, height, thumb_relative, fingerprint.phash, fingerprint.dhash,
                      fingerprint.gradient_hash, fingerprint.colorhash, image_id))
        conn.commit()
        if old_thumb and old_thumb != thumb_relative and not conn.execute(
                'SELECT 1 FROM image WHERE thumb_path = ? LIMIT 1', (old_thumb,)).fetchone():
            (library / 'thumbnails' / old_thumb).unlink(missing_ok=True)
        captioned = any(source.with_name(source.stem + suffix).is_file() for suffix in ('_nl.txt', '_moss.json', '_layout.json'))
        return {'image_id': image_id, 'folder_id': row['folder_id'], 'colour': name, 'captioned': captioned}
    finally:
        conn.close()


def apply(folder_id: Optional[int] = None, group_id: Optional[int] = None, progress=lambda **_: None) -> dict:
    from app.services.tags import TagService
    library = db.LIBRARY_PATH
    folders = _folders(folder_id, group_id)
    tags = TagService(library)
    stats = {'folders': len(folders), 'checked': 0, 'flattened': 0, 'captioned': 0, 'failed': 0, 'colours': {}, 'errors': []}
    for done, folder in enumerate(folders, 1):
        conn = db.get_connection()
        try:
            ids = [r[0] for r in conn.execute('SELECT id FROM image WHERE folder_id = ? ORDER BY id', (folder['id'],))]
        finally:
            conn.close()
        by_colour: dict[str, list[int]] = {}
        for image_id in ids:
            stats['checked'] += 1
            try:
                result = flatten_image(image_id, library)
            except Exception as exc:   # one bad file must not stop the rest; the page lists the first few
                stats['failed'] += 1
                if len(stats['errors']) < 20:
                    stats['errors'].append(f'{folder["name"]} #{image_id}: {type(exc).__name__}: {exc}'[:300])
                continue
            if result:
                stats['flattened'] += 1
                stats['captioned'] += result['captioned']
                stats['colours'][result['colour']] = stats['colours'].get(result['colour'], 0) + 1
                by_colour.setdefault(result['colour'], []).append(image_id)
            if stats['checked'] % 50 == 0:
                progress(done=done - 1, total=len(folders), checked=stats['checked'], flattened=stats['flattened'])
        flattened = [image_id for ids_ in by_colour.values() for image_id in ids_]
        if flattened:
            try:
                tags.bulk_edit(folder['id'], flattened, [REMOVED_TAG], 'remove')
                for name, image_ids in by_colour.items():
                    tags.bulk_edit(folder['id'], image_ids, ['simple background', f'{name} background'], 'add')
            except (LookupError, ValueError) as exc:   # the images are flattened; only their tags need a hand edit
                if len(stats['errors']) < 20:
                    stats['errors'].append(f'{folder["name"]}: tags not updated: {exc}'[:300])
        progress(done=done, total=len(folders), checked=stats['checked'], flattened=stats['flattened'])
    return stats


def status() -> dict:
    with _lock:
        return dict(_state)


def start(folder_id: Optional[int] = None, group_id: Optional[int] = None) -> dict:
    _folders(folder_id, group_id)   # an unknown folder fails here, before the thread starts
    with _lock:
        if _state.get('status') == 'running':
            raise RuntimeError('Transparent images are already being flattened')
        _state.clear()
        _state.update(status='running', folder_id=folder_id, group_id=group_id, done=0, total=0, checked=0, flattened=0,
                      started_at=datetime.now(timezone.utc).isoformat())

    def progress(**values):
        with _lock:
            _state.update(values)

    def work():
        try:
            result = apply(folder_id, group_id, progress)
            with _lock:
                _state.update(status='done', result=result, finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:   # images flattened so far stay flattened (each one is complete on its own)
            with _lock:
                _state.update(status='error', error=f'{type(exc).__name__}: {exc}')

    threading.Thread(target=work, name='flatten-transparent', daemon=True).start()
    return status()
