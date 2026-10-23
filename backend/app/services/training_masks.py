"""Loss masks for the trainer's masked training: black over an image's watermarks, signatures and URLs, white
elsewhere, so the model is never rewarded for drawing them. A watermark most of an artist's images share otherwise
sticks to the style, whatever the captions and negative prompts say.

The boxes come from the captioning script's annotations: the `watermark` list of `<image>_moss.json` (detector
boxes the model confirmed, or the model's own) and any `watermark` items of `<image>_layout.json`, all in MOSS's
0-1000 [y_min, x_min, y_max, x_max] space. The trainer needs a mask for every image of a masked folder, so a folder
with any boxed image gets one per image (all white where there is nothing to hide); folders without any get none
and train unmasked.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

MASK_MAX_SIDE = 512     # masks only need the image's frame: the trainer resizes them to each bucket
MASK_MIN_SIDE = 128     # ...but a short side this long keeps the aspect ratio within the trainer's 1% check
BOX_MARGIN = 0.01       # added around each box, as a share of the image side (the config dilates a little more)


def _valid(box) -> bool:
    return (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
            and box[2] > box[0] and box[3] > box[1])


def watermark_boxes(image_path: Path) -> list[list[float]]:
    """The image's watermark / signature / URL boxes, 0-1000 [y_min, x_min, y_max, x_max], without duplicates."""
    boxes: list[list[float]] = []
    for suffix in ('_moss.json', '_layout.json'):
        path = image_path.with_name(image_path.stem + suffix)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        entries = data.get('watermark') if suffix == '_moss.json' else [
            item for item in data.get('items') or [] if isinstance(item, dict) and item.get('label') == 'watermark']
        for entry in entries or []:
            box = entry.get('box_2d') if isinstance(entry, dict) else None
            if _valid(box) and [float(v) for v in box] not in boxes:
                boxes.append([float(v) for v in box])
    return boxes


def mask_size(width: int, height: int) -> tuple[int, int]:
    """A small mask of the image's own frame."""
    scale = min(1.0, max(MASK_MAX_SIDE / max(width, height), MASK_MIN_SIDE / max(1, min(width, height))))
    return max(1, round(width * scale)), max(1, round(height * scale))


def draw_mask(width: int, height: int, boxes: list[list[float]]) -> Image.Image:
    w, h = mask_size(width, height)
    mask = Image.new('L', (w, h), 255)
    draw = ImageDraw.Draw(mask)
    for y0, x0, y1, x1 in boxes:
        dy, dx = BOX_MARGIN * 1000, BOX_MARGIN * 1000
        left, top = max(0.0, x0 - dx) / 1000 * w, max(0.0, y0 - dy) / 1000 * h
        right, bottom = min(1000.0, x1 + dx) / 1000 * w, min(1000.0, y1 + dy) / 1000 * h
        draw.rectangle((int(left), int(top), int(right + 0.999), int(bottom + 0.999)), fill=0)
    return mask


def write_folder_masks(images: list[tuple[Path, int, int]], out_dir: Path) -> int:
    """Masks for one folder's (image path, width, height) list. Returns how many images have boxes; writes nothing
    (and clears out_dir) when none has."""
    boxed = [(path, width, height, watermark_boxes(path)) for path, width, height in images]
    if out_dir.is_dir():
        for old in out_dir.glob('*.png'):   # images removed since the last export must not keep a mask
            old.unlink()
    count = sum(1 for *_, boxes in boxed if boxes)
    if not count:
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for path, width, height, boxes in boxed:
        draw_mask(width, height, boxes).save(out_dir / f'{path.stem}.png', optimize=True)
    return count
