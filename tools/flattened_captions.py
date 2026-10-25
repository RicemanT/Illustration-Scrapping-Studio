"""Find the captioned images that "Flatten transparent images" changed, show what their captions say about the
background, and optionally send them back for captioning.

Flattening puts a transparent image on a solid colour, so a caption written before it may still describe a
transparent (or white, or checkered) background. Every flattened image has its original in
<library>/.trash/transparent-originals/, which is how they are found.

    python tools/flattened_captions.py --library /home/jovyan/Artist-Collection-Dataset
        writes flattened_captions.csv (folder, image, background colour now, caption files, background mentions)
        and prints a summary.
    python tools/flattened_captions.py --library ... --recaption
        also moves each listed image's _moss.json, _nl.txt and _layout.json to <library>/.trash/recaption/, so the
        captioning script's "caption" and "layout" modes treat it as new. Tags (.txt) and detections (_ocr.json)
        stay. --only-mentions limits it to captions that mention the background at all.

Standard library only; run it with the app stopped or idle.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path

CAPTION_FILES = ('_moss.json', '_nl.txt', '_layout.json')
MENTION = re.compile(r'[^.!?\n]*\b(transparen\w*|background|backdrop|checker\w*|alpha|cut-?out)\b[^.!?\n]*[.!?]?', re.IGNORECASE)


def caption_texts(image: Path) -> list[str]:
    """Every caption string of the image: the NL caption, the three MOSS lengths and the layout's captions."""
    texts = []
    nl = image.with_name(image.stem + '_nl.txt')
    if nl.is_file():
        texts.append(nl.read_text(encoding='utf-8', errors='replace'))
    for suffix in ('_moss.json', '_layout.json'):
        path = image.with_name(image.stem + suffix)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            continue
        if suffix == '_moss.json':
            texts += [v for v in (data.get('caption') or {}).values() if isinstance(v, str)]
        else:
            texts.append(str((data.get('summary') or {}).get('caption') or ''))
            texts += [str(item.get('caption') or '') for item in data.get('items') or [] if isinstance(item, dict)]
    return [t for t in texts if t.strip()]


def background_tag(image: Path) -> str:
    tags = image.with_suffix('.txt')
    if tags.is_file():
        for tag in tags.read_text(encoding='utf-8', errors='replace').split(','):
            tag = tag.strip()
            if tag.endswith(' background') and tag != 'simple background':
                return tag
    return ''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--library', required=True, help='the Studio library folder (the one with images/ and .trash/)')
    parser.add_argument('--out', default='flattened_captions.csv')
    parser.add_argument('--recaption', action='store_true', help="move the listed images' caption files aside")
    parser.add_argument('--only-mentions', action='store_true', help='with --recaption: only captions that mention the background')
    args = parser.parse_args(argv)
    library = Path(args.library)
    originals = library / '.trash' / 'transparent-originals'
    if not originals.is_dir():
        print(f'No flattened images: {originals} does not exist.', file=sys.stderr)
        return 1
    rows, moved = [], 0
    for backup in sorted(p for p in originals.rglob('*') if p.is_file()):
        relative = backup.relative_to(originals)
        image = library / 'images' / relative
        present = [s for s in CAPTION_FILES if image.with_name(image.stem + s).is_file()]
        if not image.is_file() or not present:
            continue
        mentions = sorted({m.group(0).strip() for text in caption_texts(image) for m in MENTION.finditer(text)})
        rows.append({'folder': relative.parent.as_posix(), 'image': relative.name, 'now': background_tag(image),
                     'caption_files': ' '.join(present), 'mentions': ' | '.join(mentions)})
        if args.recaption and (mentions or not args.only_mentions):
            target = library / '.trash' / 'recaption' / relative.parent
            target.mkdir(parents=True, exist_ok=True)
            for suffix in present:
                source = image.with_name(image.stem + suffix)
                shutil.move(str(source), str(target / source.name))
            moved += 1
    with open(args.out, 'w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, ['folder', 'image', 'now', 'caption_files', 'mentions'])
        writer.writeheader()
        writer.writerows(rows)
    with_mentions = sum(1 for r in rows if r['mentions'])
    folders = sorted({r['folder'] for r in rows})
    print(f'{len(rows)} flattened images have captions, in {len(folders)} folders; {with_mentions} mention the background.')
    print(f'Report: {Path(args.out).resolve()}')
    for row in [r for r in rows if r['mentions']][:5]:
        print(f"  {row['folder']}/{row['image']} (now {row['now'] or '?'}): {row['mentions'][:160]}")
    if args.recaption:
        print(f'{moved} images sent back for captioning: their caption files are in {library / ".trash" / "recaption"}. '
              f'Run the captioning script in "caption" mode, then "layout", on these folders.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
