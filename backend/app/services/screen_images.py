"""Screen-sized copies of library images for phones and tablets.

Originals can be 5-20 MB PNGs, far more than a phone needs to fill its screen. The touch interface asks for a
JPEG at most `size` pixels on the long side (about 200 KB at 1080), made once and kept in
`<library>/.cache/screen/<image id>-<size>-<file mtime>.jpg`, so an edited original gets a fresh copy.
Animated GIFs are served as they are (they stay animated), and files Pillow cannot read fall back to the
thumbnail.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import app.db as db

SIZES = (720, 1080, 1440, 2048)


def _row(image_id: int):
    conn = db.get_connection()
    try:
        return conn.execute('SELECT path, thumb_path FROM image WHERE id=?', (image_id,)).fetchone()
    finally:
        conn.close()


def nearest_size(size: int) -> int:
    return min(SIZES, key=lambda s: (abs(s - size), s))


def screen_file(image_id: int, size: int = 1080) -> Optional[tuple[Path, str]]:
    """(file, media type) to send, or None when the image does not exist."""
    row = _row(image_id)
    if not row:
        return None
    images = (db.LIBRARY_PATH / 'images').resolve()
    original = (images / row['path']).resolve()
    if images not in original.parents or not original.is_file():
        return None
    if original.suffix.lower() == '.gif':
        return original, 'image/gif'
    size = nearest_size(size)
    cache = db.LIBRARY_PATH / '.cache' / 'screen'
    target = cache / f'{image_id}-{size}-{original.stat().st_mtime_ns}.jpg'
    if target.is_file():
        return target, 'image/jpeg'
    try:
        from PIL import Image, ImageOps
        with Image.open(original) as raw:
            raw.seek(0)
            image = ImageOps.exif_transpose(raw)
            if image.mode in ('RGBA', 'LA', 'PA') or (image.mode == 'P' and 'transparency' in image.info):
                rgba = image.convert('RGBA')
                flat = Image.new('RGB', rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel('A'))
                image = flat
            else:
                image = image.convert('RGB')
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            cache.mkdir(parents=True, exist_ok=True)
            for old in cache.glob(f'{image_id}-{size}-*.jpg'):
                old.unlink(missing_ok=True)
            partial = target.with_name(target.name + '.part')
            image.save(partial, format='JPEG', quality=85, progressive=True, optimize=True)
            partial.replace(target)
        return target, 'image/jpeg'
    except Exception:
        thumb = (db.LIBRARY_PATH / 'thumbnails' / row['thumb_path']).resolve() if row['thumb_path'] else None
        if thumb and thumb.is_file():
            return thumb, 'image/jpeg'
        return original, None
