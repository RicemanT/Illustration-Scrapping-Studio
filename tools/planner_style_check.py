"""Flag off-style images in a Dataset Planner download.

For every artist in a planner download, each delivered image is reduced to
style statistics: the per-channel mean and standard deviation of VGG16
feature maps at four depths, the representation behind neural style
transfer. Images far from their artist's median style (a robust z-score over
cosine distances) are flagged: typically sketches, photos, 3D renders, edits
or guest art. Flags appear in the Planner's Review section, where you decide
what to ban; `--ban` bans every flagged post instead.

These statistics are dominated by colour palette: an artist's rare pastel
piece can be flagged while a screenshot with app UI in it is not. Treat flags
as suggestions, and compare with `--grayscale`, which judges line work,
shading and texture only.

The app does not depend on PyTorch. Run this script in any Python environment
that has torch, torchvision, numpy and Pillow, for example an existing
ComfyUI or training environment:

    python tools/planner_style_check.py --library /path/to/library
    python tools/planner_style_check.py --library /path/to/library --delivery 2 --device cuda:1 --pause 0.2

It is light: one GPU, small batches and roughly a minute per few thousand
images. `--pause` waits between batches to keep a shared GPU cool. Run it
while the planner download is not running.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# relu1_1, relu2_1, relu3_1, relu4_1 in torchvision's VGG16 `features`.
STYLE_LAYERS = (1, 6, 11, 18)
DELIVERED = ('done', 'skipped')
PREFETCH = 3


def style_outliers(vectors: np.ndarray, threshold: float, min_images: int):
    """Cosine distance to the artist's median style and its robust z-score.

    Returns (distances, scores, flags). Artists with fewer than `min_images`
    images are never flagged: their median is not a reliable reference.
    """
    count = len(vectors)
    if count == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0, dtype=bool)
    unit = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    center = np.median(unit, axis=0)
    center /= max(float(np.linalg.norm(center)), 1e-12)
    distances = 1.0 - unit @ center
    median = float(np.median(distances))
    spread = float(np.median(np.abs(distances - median))) * 1.4826
    scores = (distances - median) / max(spread, 1e-6)
    flags = scores > threshold if count >= min_images else np.zeros(count, dtype=bool)
    return distances, scores, flags


def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load_items(library: Path, database: Path, planner_db: Path, delivery_id: int | None):
    """Delivered images of a planner download: artist, post and file path."""
    planner = connect_ro(planner_db)
    try:
        if delivery_id is None:
            row = planner.execute('SELECT id FROM delivery ORDER BY id DESC LIMIT 1').fetchone()
            if not row:
                raise SystemExit('No planner download found. Download a run first.')
            delivery_id = row['id']
        marks = ','.join('?' * len(DELIVERED))
        items = planner.execute(f"""SELECT d.site, d.remote_id, d.artist_id, d.folder_id, a.display_name
                                    FROM delivery_item d JOIN artist a ON a.id=d.artist_id
                                    WHERE d.delivery_id=? AND d.status IN ({marks})""", (delivery_id, *DELIVERED)).fetchall()
    finally:
        planner.close()
    wanted = defaultdict(dict)
    for item in items:
        wanted[item['folder_id']][(item['site'], item['remote_id'])] = item
    main = connect_ro(database)
    images = []
    try:
        for folder_id, posts in wanted.items():
            seen = set()
            for row in main.execute("""SELECT i.id, i.path, s.provider, s.remote_id FROM image i
                                       JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?""", (folder_id,)):
                key = (row['provider'], str(row['remote_id']).split(':', 1)[0])
                item = posts.get(key)
                if item is None or row['id'] in seen:
                    continue
                seen.add(row['id'])
                images.append({'artist_id': item['artist_id'], 'artist': item['display_name'], 'site': item['site'],
                               'remote_id': item['remote_id'], 'image_id': row['id'], 'path': library / 'images' / row['path']})
    finally:
        main.close()
    return delivery_id, images


class StyleEncoder:
    def __init__(self, device: str, size: int, grayscale: bool = False):
        import torch
        import torchvision
        self.torch = torch
        self.device = torch.device(device)
        self.size = size
        self.grayscale = grayscale
        weights = torchvision.models.VGG16_Weights.IMAGENET1K_V1
        net = torchvision.models.vgg16(weights=weights).features[:max(STYLE_LAYERS) + 1].eval().to(self.device)
        self.half = self.device.type == 'cuda'
        self.net = net.half() if self.half else net
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.device).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.device).view(1, 3, 1, 1)

    def load(self, path: Path):
        from PIL import Image
        try:
            with Image.open(path) as image:
                image = image.convert('L' if self.grayscale else 'RGB').convert('RGB').resize((self.size, self.size), Image.BICUBIC)
                return np.asarray(image, dtype=np.uint8)
        except Exception as exc:  # unreadable files are reported, not fatal
            print(f'  skipped unreadable {path}: {exc}', file=sys.stderr)
            return None

    def encode(self, arrays: list[np.ndarray]) -> np.ndarray:
        torch = self.torch
        with torch.inference_mode():
            batch = torch.from_numpy(np.stack(arrays)).to(self.device).permute(0, 3, 1, 2).float().div(255)
            batch = (batch - self.mean) / self.std
            x = batch.half() if self.half else batch
            parts = []
            for index, layer in enumerate(self.net):
                x = layer(x)
                if index in STYLE_LAYERS:
                    flat = x.float().flatten(2)
                    stats = torch.cat([flat.mean(2), flat.std(2)], dim=1)
                    # Equal weight per depth regardless of channel count.
                    parts.append(torch.nn.functional.normalize(stats, dim=1))
            return torch.cat(parts, dim=1).cpu().numpy()


def write_results(planner_db: Path, results: list[dict], artist_ids: set[int], ban: bool) -> int:
    conn = sqlite3.connect(planner_db, timeout=60)
    try:
        conn.executescript("""CREATE TABLE IF NOT EXISTS style_flag (
            artist_id INTEGER NOT NULL, site TEXT NOT NULL, remote_id TEXT NOT NULL,
            distance REAL NOT NULL, score REAL NOT NULL, checked_at TEXT NOT NULL,
            PRIMARY KEY (artist_id, site, remote_id))""")
        checked_at = datetime.now(timezone.utc).isoformat()
        conn.executemany('DELETE FROM style_flag WHERE artist_id=?', [(a,) for a in artist_ids])
        flagged = [r for r in results if r['flagged']]
        conn.executemany('INSERT OR REPLACE INTO style_flag VALUES (?,?,?,?,?,?)',
                         [(r['artist_id'], r['site'], r['remote_id'], r['distance'], r['score'], checked_at) for r in flagged])
        banned = 0
        if ban:
            locked = {(r[0], r[1], r[2]) for r in conn.execute("SELECT artist_id, site, remote_id FROM override WHERE action='lock'")}
            for r in flagged:
                if (r['artist_id'], r['site'], r['remote_id']) not in locked:
                    conn.execute("INSERT OR REPLACE INTO override (artist_id, site, remote_id, action, created_at) VALUES (?,?,?,'ban',?)",
                                 (r['artist_id'], r['site'], r['remote_id'], checked_at))
                    banned += 1
        conn.commit()
        return banned
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--library', required=True, type=Path, help='the app library folder (contains index.db and images/)')
    parser.add_argument('--database', type=Path, help='index.db location if not inside the library')
    parser.add_argument('--planner', type=Path, help='planner folder if not <library>/planner')
    parser.add_argument('--delivery', type=int, help='planner download ID (default: the latest)')
    parser.add_argument('--device', default='cuda:0', help='torch device, for example cuda:1 or cpu')
    parser.add_argument('--batch', type=int, default=16)
    parser.add_argument('--size', type=int, default=384, help='images are resized to size × size before encoding')
    parser.add_argument('--threshold', type=float, default=3.5, help='robust z-score above which an image is flagged')
    parser.add_argument('--min-images', type=int, default=8, help='do not flag artists with fewer delivered images')
    parser.add_argument('--pause', type=float, default=0.0, help='seconds to wait between batches (keeps a shared GPU cool)')
    parser.add_argument('--grayscale', action='store_true',
                        help='ignore colour: judge line work, shading and texture only (colour statistics otherwise dominate)')
    parser.add_argument('--ban', action='store_true', help='also ban every flagged post (locked posts are skipped)')
    args = parser.parse_args(argv)

    library = args.library.expanduser().resolve()
    database = (args.database or library / 'index.db').expanduser().resolve()
    planner_dir = (args.planner or library / 'planner').expanduser().resolve()
    planner_db = planner_dir / 'planner.db'
    for path in (database, planner_db):
        if not path.exists():
            raise SystemExit(f'Not found: {path}')
    delivery_id, images = load_items(library, database, planner_db, args.delivery)
    if not images:
        raise SystemExit(f'Download {delivery_id} has no delivered images yet.')
    print(f'Download {delivery_id}: {len(images)} images from {len({i["artist_id"] for i in images})} artists')

    encoder = StyleEncoder(args.device, args.size, args.grayscale)
    vectors, kept = [], []
    started = time.monotonic()
    batches = [images[i:i + args.batch] for i in range(0, len(images), args.batch)]
    with ThreadPoolExecutor(max_workers=4) as decoders, ThreadPoolExecutor(max_workers=PREFETCH) as pool:
        # Decode a few batches ahead without holding the whole dataset in memory.
        load = lambda batch: list(decoders.map(encoder.load, [item['path'] for item in batch]))
        ahead = [pool.submit(load, batch) for batch in batches[:PREFETCH]]
        for number, batch in enumerate(batches, start=1):
            arrays = ahead.pop(0).result()
            if number - 1 + PREFETCH < len(batches):
                ahead.append(pool.submit(load, batches[number - 1 + PREFETCH]))
            pairs = [(item, array) for item, array in zip(batch, arrays) if array is not None]
            if pairs:
                vectors.append(encoder.encode([array for _, array in pairs]))
                kept.extend(item for item, _ in pairs)
            if number % 50 == 0 or number == len(batches):
                rate = len(kept) / max(time.monotonic() - started, 1e-6)
                print(f'  {len(kept)}/{len(images)} images, {rate:.0f} images/s')
            if args.pause:
                time.sleep(args.pause)
    matrix = np.concatenate(vectors) if vectors else np.zeros((0, 1))

    by_artist = defaultdict(list)
    for index, item in enumerate(kept):
        by_artist[item['artist_id']].append(index)
    per_post = defaultdict(list)
    for artist_id, indexes in by_artist.items():
        distances, scores, _ = style_outliers(matrix[indexes], args.threshold, args.min_images)
        for position, index in enumerate(indexes):
            item = kept[index]
            per_post[(artist_id, item['site'], item['remote_id'])].append((float(distances[position]), float(scores[position]), item))
    results = []
    for (artist_id, site, remote_id), values in per_post.items():
        # A video or animation yields several frames; judge the post by their mean.
        distance = sum(v[0] for v in values) / len(values)
        score = sum(v[1] for v in values) / len(values)
        enough = len(by_artist[artist_id]) >= args.min_images
        results.append({'artist_id': artist_id, 'artist': values[0][2]['artist'], 'site': site, 'remote_id': remote_id,
                        'distance': round(distance, 5), 'score': round(score, 3), 'flagged': enough and score > args.threshold,
                        'path': str(values[0][2]['path'])})
    results.sort(key=lambda r: (r['artist'], -r['score']))

    export = planner_dir / 'exports' / f'style-check-download-{delivery_id}.csv'
    export.parent.mkdir(parents=True, exist_ok=True)
    with open(export, 'w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, ['site', 'artist', 'remote_id', 'score', 'distance', 'flagged', 'path'], extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        writer.writerows(results)
    banned = write_results(planner_db, results, set(by_artist), args.ban)
    flagged = sum(r['flagged'] for r in results)
    print(f'Flagged {flagged} of {len(results)} posts across {len(by_artist)} artists. Details: {export}')
    if args.ban:
        print(f'Banned {banned} posts. Run the plan again, download it, then remove images the run no longer selects.')
    else:
        print('Review flagged images in the Planner (filter: artists with style flags), or rerun with --ban.')


if __name__ == '__main__':
    main()
