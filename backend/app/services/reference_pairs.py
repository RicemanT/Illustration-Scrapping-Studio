"""Reference-pair export for training a MOSS-Image reference adapter (see the trainer's docs/moss-reference-adapter.md).

From curated folders it writes `pairs.jsonl`: a target image, a reference image (optionally a crop of one character
from the layout's boxes) and the target's tags split into what the prompt keeps and what the reference must supply.
One reference slot carries whatever the prompt leaves out:

    identity_style  same character, same artist     prompt drops the trigger, the character and its usual look
    identity        same character, another artist  prompt keeps the target's trigger, drops the character and look
    style           same artist, no shared character prompt drops only the trigger

A character's usual look is the general and species tags on at least LOOK_SHARE of its single-character images (at
least two of them), at most LOOK_MAX: hair, eyes, ears, outfit, species. Count, rating, composition and quality tags are
never part of it. A few characters (hashed, with enough images and artists) are held out for evaluation: no pair uses
them, and `eval.jsonl` lists their images.

The trainer composes each prompt per step: `keep` alone (70%), plus a random 30-70% of `look` (20%), or `minimal` (10%).
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import app.db as db

LOOK_SHARE = 0.4
LOOK_MAX = 18
REFS_PER_TARGET = 3          # references per (target, pair kind)
HOLDOUT_SHARE = 0.03         # characters held out for evaluation, among those with enough images
NEVER_LOOK = {'solo', 'looking at viewer', 'simple background', 'white background', 'full body', 'upper body',
              'cowboy shot', 'portrait', 'smile', 'open mouth', 'blush', 'standing', 'sitting', 'closed mouth',
              'highres', 'absurdres', 'commentary', 'commentary request', 'english commentary', 'signature',
              'watermark', 'artist name', 'dated', 'twitter username', 'web address', 'patreon username'}
COUNT_TAGS = {'1girl', '2girls', '3girls', '4girls', '5girls', '6+girls', 'multiple girls', '1boy', '2boys', '3boys',
              '4boys', '5boys', '6+boys', 'multiple boys', '1other', '2others', 'solo', 'solo focus', 'duo', 'trio',
              'group', 'female', 'male', 'ambiguous gender', 'intersex', 'anthro', 'feral', 'human'}
RATING_TAGS = {'general', 'sensitive', 'questionable', 'explicit', 'safe', 'rating:general', 'rating:sensitive',
               'rating:questionable', 'rating:explicit'}

_lock = threading.Lock()
_state: dict = {'status': 'idle'}


def _folders(group_ids: Optional[list[int]], folder_ids: Optional[list[int]]) -> list[dict]:
    conn = db.get_connection()
    try:
        if folder_ids:
            rows = conn.execute(f"SELECT id, name, slug FROM collection WHERE id IN ({','.join('?' * len(folder_ids))})", folder_ids)
        elif group_ids:
            rows = conn.execute(f"SELECT id, name, slug FROM collection WHERE type='artist' AND group_id IN ({','.join('?' * len(group_ids))})", group_ids)
        else:
            rows = conn.execute("SELECT id, name, slug FROM collection WHERE type='artist'")
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _character_boxes(image_path: Path) -> dict[str, list[float]]:
    """{character tag: box_2d} from the image's layout, for characters the layout tags."""
    path = image_path.with_name(image_path.stem + '_layout.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    boxes = {}
    for item in data.get('items') or []:
        if isinstance(item, dict) and item.get('label') == 'character' and isinstance(item.get('box_2d'), list):
            for tag in item.get('tags') or []:
                boxes.setdefault(str(tag).replace('_', ' ').casefold(), item['box_2d'])
    return boxes


def collect(folders: list[dict]) -> list[dict]:
    """One record per image: its folder's artist, tags by category, quality tags, character boxes."""
    from app.services.tags import TagService
    service = TagService(db.LIBRARY_PATH)
    images_root = db.LIBRARY_PATH / 'images'
    records = []
    conn = db.get_connection()
    try:
        for folder in folders:
            for image_id, relative in conn.execute('SELECT id, path FROM image WHERE folder_id=? ORDER BY id', (folder['id'],)).fetchall():
                categories = service._source_category_map(conn, image_id, service._included_categories(conn, image_id))
                tags = service._effective_tags(conn, image_id)
                quality = set(t.casefold() for t in service._quality_tags(conn, image_id))
                by = defaultdict(list)
                for tag in tags:
                    key = tag.casefold()
                    if key in quality:
                        by['quality'].append(tag)
                    else:
                        by[categories.get(key, 'general')].append(tag)
                records.append({'image_id': image_id, 'path': relative.replace('\\', '/'), 'folder': folder['id'],
                                'artist': folder['name'], 'tags': tags, 'by': dict(by),
                                'characters': [t.casefold() for t in by.get('character', [])],
                                'boxes': _character_boxes(images_root / relative)})
    finally:
        conn.close()
    return records


def character_looks(records: list[dict]) -> dict[str, list[str]]:
    """Each character's usual look from its single-character images."""
    seen, counts = Counter(), defaultdict(Counter)
    for r in records:
        if len(r['characters']) != 1:
            continue
        character = r['characters'][0]
        seen[character] += 1
        for tag in r['by'].get('general', []) + r['by'].get('species', []):
            key = tag.casefold()
            if key not in NEVER_LOOK and key not in COUNT_TAGS and key not in RATING_TAGS:
                counts[character][key] += 1
    looks = {}
    for character, n in seen.items():
        needed = max(2, LOOK_SHARE * n)
        looks[character] = [t for t, c in counts[character].most_common() if c >= needed][:LOOK_MAX]
    return looks


def _split(record: dict, drop_trigger: bool, character: Optional[str], look: list[str]) -> dict:
    """The target's tags as keep / look / minimal for one pair."""
    look_keys = set(look)
    drop = set()
    if drop_trigger:
        drop.update(t.casefold() for t in record['by'].get('artist', []))
    if character:
        drop.add(character)
        drop.update(t.casefold() for t in record['by'].get('copyright', []))
    keep, removed = [], []
    for tag in record['tags']:
        key = tag.casefold()
        if key in drop:
            continue
        (removed if character and key in look_keys else keep).append(tag)
    minimal = [t for t in keep if t.casefold() in COUNT_TAGS or t.casefold() in RATING_TAGS
               or t in record['by'].get('quality', []) or (not drop_trigger and t in record['by'].get('artist', []))]
    return {'keep': keep, 'look': removed, 'minimal': minimal}


def build(records: list[dict], seed: int = 0) -> dict:
    rng = random.Random(seed)
    looks = character_looks(records)
    by_character = defaultdict(list)
    for r in records:
        for c in r['characters']:
            by_character[c].append(r)
    # Held-out characters: enough images and more than one artist, picked by hash so reruns agree.
    candidates = sorted(c for c, rs in by_character.items() if len(rs) >= 5 and len({r['artist'] for r in rs}) >= 2)
    holdout = {c for c in candidates if int(hashlib.md5(c.encode()).hexdigest()[:8], 16) % 1000 < HOLDOUT_SHARE * 1000}
    pairs = []

    def reference(r, character):
        """A reference record for `character` (a crop box when the image shows others too), or None."""
        if len(r['characters']) == 1:
            return {'path': r['path'], 'crop': None}
        box = r['boxes'].get(character)
        return {'path': r['path'], 'crop': box} if box else None

    for character, group in by_character.items():
        if character in holdout or len(group) < 2:
            continue
        for target in group:
            if target['characters'] != [character]:
                continue   # targets show only this character, so the prompt's other content stays coherent
            others = [r for r in group if r is not target]
            for kind, pool in (('identity_style', [r for r in others if r['artist'] == target['artist']]),
                               ('identity', [r for r in others if r['artist'] != target['artist']])):
                refs = [ref for r in rng.sample(pool, len(pool)) if (ref := reference(r, character))][:REFS_PER_TARGET]
                for ref in refs:
                    pairs.append({'kind': kind, 'character': character, 'target': target['path'], 'reference': ref,
                                  **_split(target, kind == 'identity_style', character, looks.get(character, []))})
    by_artist = defaultdict(list)
    for r in records:
        by_artist[r['artist']].append(r)
    for artist, group in by_artist.items():
        for target in group:
            if holdout.intersection(target['characters']):
                continue
            pool = [r for r in group if r is not target and not set(r['characters']) & set(target['characters'])
                    and not holdout.intersection(r['characters'])]
            for r in rng.sample(pool, min(REFS_PER_TARGET, len(pool))):
                pairs.append({'kind': 'style', 'character': None, 'target': target['path'],
                              'reference': {'path': r['path'], 'crop': None}, **_split(target, True, None, [])})
    evaluation = [{'character': c, 'images': [r['path'] for r in by_character[c]], 'look': looks.get(c, [])} for c in sorted(holdout)]
    return {'pairs': pairs, 'eval': evaluation, 'looks': looks,
            'counts': dict(Counter(p['kind'] for p in pairs)), 'held_out': sorted(holdout)}


def export(group_ids: Optional[list[int]] = None, folder_ids: Optional[list[int]] = None, name: str = 'reference-pairs') -> dict:
    from app.services.planner_store import planner_dir
    folders = _folders(group_ids, folder_ids)
    records = collect(folders)
    result = build(records)
    target = planner_dir() / 'exports' / name
    target.mkdir(parents=True, exist_ok=True)
    with open(target / 'pairs.jsonl', 'w', encoding='utf-8') as fh:
        for pair in result['pairs']:
            fh.write(json.dumps(pair, ensure_ascii=False) + '\n')
    with open(target / 'eval.jsonl', 'w', encoding='utf-8') as fh:
        for item in result['eval']:
            fh.write(json.dumps(item, ensure_ascii=False) + '\n')
    (target / 'looks.json').write_text(json.dumps(result['looks'], ensure_ascii=False, indent=1), encoding='utf-8')
    summary = {'folders': len(folders), 'images': len(records), 'pairs': len(result['pairs']), 'by_kind': result['counts'],
               'held_out_characters': result['held_out'], 'path': str(target),
               'created_at': datetime.now(timezone.utc).isoformat()}
    (target / 'README.txt').write_text(
        'Reference pairs for a MOSS-Image reference adapter (Illustration Scrapping Studio).\n'
        'Paths are relative to the library images folder, like folders.csv in the training export.\n\n'
        '  pairs.jsonl  one pair per line: kind, character, target, reference {path, crop (0-1000 yxyx or null)},\n'
        '               keep (prompt tags), look (tags the reference supplies; 20% of prompts keep 30-70% of them),\n'
        '               minimal (10% of prompts)\n'
        '  eval.jsonl   held-out characters: their images and usual look, never in a pair\n'
        '  looks.json   every character\'s usual look\n\n' + json.dumps(summary, indent=1) + '\n', encoding='utf-8')
    return summary


def status() -> dict:
    with _lock:
        return dict(_state)


def start(group_ids: Optional[list[int]] = None, folder_ids: Optional[list[int]] = None, name: str = 'reference-pairs') -> dict:
    with _lock:
        if _state.get('status') == 'running':
            raise RuntimeError('Reference pairs are already being exported')
        _state.clear()
        _state.update(status='running', started_at=datetime.now(timezone.utc).isoformat())

    def work():
        try:
            result = export(group_ids, folder_ids, name)
            with _lock:
                _state.update(status='done', result=result, finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            with _lock:
                _state.update(status='error', error=f'{type(exc).__name__}: {exc}')

    threading.Thread(target=work, name='reference-pairs', daemon=True).start()
    return status()
