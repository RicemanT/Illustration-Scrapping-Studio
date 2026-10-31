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

Pairs are written as they are made, so a whole library (hundreds of thousands of images, millions of pairs) fits in
memory; references come from per-character and per-artist pools, sampled without scanning the whole pool per target.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

import app.db as db

LOOK_SHARE = 0.4
LOOK_MAX = 18
REFS_PER_TARGET = 3          # references per (target, pair kind)
HOLDOUT_SHARE = 0.03         # characters held out for evaluation, among those with enough images
EXACT_POOL = 200             # pools up to this size are filtered exactly; larger ones are sampled
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


def _accepted_folder_ids() -> set[int]:
    from app.services.planner_curation import _accepted_folders
    try:
        return set(_accepted_folders())
    except Exception:   # no planner database: nothing has been accepted
        return set()


def _folders(group_ids: Optional[list[int]], folder_ids: Optional[list[int]], accepted_only: bool = False) -> list[dict]:
    conn = db.get_connection()
    try:
        if folder_ids:
            rows = conn.execute(f"SELECT id, name, slug FROM collection WHERE id IN ({','.join('?' * len(folder_ids))})", folder_ids)
        elif group_ids:
            rows = conn.execute(f"SELECT id, name, slug FROM collection WHERE type='artist' AND group_id IN ({','.join('?' * len(group_ids))})", group_ids)
        else:
            rows = conn.execute("SELECT id, name, slug FROM collection WHERE type='artist'")
        folders = [dict(r) for r in rows]
    finally:
        conn.close()
    if accepted_only:
        accepted = _accepted_folder_ids()
        folders = [f for f in folders if f['id'] in accepted]
    return folders


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
                boxes.setdefault(sys.intern(str(tag).replace('_', ' ').casefold()), item['box_2d'])
    return boxes


def collect(folders: list[dict], progress: Optional[Callable[[int], None]] = None) -> list[dict]:
    """One record per image: its folder's artist, tags by category, quality tags, character boxes. Tag names are
    interned: across a whole library the same few thousand strings repeat millions of times."""
    from app.services.tags import TagService
    service = TagService(db.LIBRARY_PATH)
    images_root = db.LIBRARY_PATH / 'images'
    records = []
    conn = db.get_connection()
    try:
        for folder in folders:
            for image_id, relative in conn.execute('SELECT id, path FROM image WHERE folder_id=? ORDER BY id', (folder['id'],)).fetchall():
                categories = service._source_category_map(conn, image_id, service._included_categories(conn, image_id))
                tags = [sys.intern(t) for t in service._effective_tags(conn, image_id)]
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
                                'characters': [sys.intern(t.casefold()) for t in by.get('character', [])],
                                'boxes': _character_boxes(images_root / relative)})
                if progress and len(records) % 1000 == 0:
                    progress(len(records))
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


def _pick(rng: random.Random, pool: list, accept: Callable[[object], bool], k: int) -> list:
    """Up to k distinct pool items that `accept`, in random order. Small pools are filtered exactly (every acceptable
    item has a chance); large ones are sampled, a few tries per wanted item, so a target costs O(k), not O(pool)."""
    if len(pool) <= EXACT_POOL:
        return [item for item in rng.sample(pool, len(pool)) if accept(item)][:k]
    chosen, seen = [], set()
    for _ in range(k * 8):
        item = pool[rng.randrange(len(pool))]
        if id(item) not in seen and accept(item):
            seen.add(id(item))
            chosen.append(item)
            if len(chosen) == k:
                break
    return chosen


def identity(character: str) -> str:
    """The character a tag names, without outfit qualifiers: 'mejiro mcqueen (frontline elegance) (umamusume)' ->
    'mejiro mcqueen (umamusume)'. The last parenthesis is the series and stays, so same-named characters stay apart."""
    return re.sub(r'(?: \([^()]*\))+( \([^()]*\))$', r'\1', character)


def _plan(records: list[dict]) -> dict:
    """Looks, per-character images, held-out characters and coverage counts."""
    looks = character_looks(records)
    by_character = defaultdict(list)
    for r in records:
        for c in r['characters']:
            by_character[c].append(r)
    artists_of = {c: len({r['artist'] for r in rs}) for c, rs in by_character.items()}
    # Held-out characters: enough images and more than one artist, picked by hash so reruns agree. Outfit variants
    # share an identity, so they are held out together: otherwise the base tag trains the "unseen" character.
    variants = defaultdict(list)
    for c in by_character:
        variants[identity(c)].append(c)
    held = {i for i, cs in variants.items()
            if len({id(r) for c in cs for r in by_character[c]}) >= 5 and len({r['artist'] for c in cs for r in by_character[c]}) >= 2
            and int(hashlib.md5(i.encode()).hexdigest()[:8], 16) % 1000 < HOLDOUT_SHARE * 1000}
    holdout = {c for i in held for c in variants[i]}
    trained = [c for c in by_character if c not in holdout]
    coverage = {'characters': len(trained),
                'characters_2_plus_images': sum(len(by_character[c]) >= 2 for c in trained),
                'characters_2_plus_artists': sum(artists_of[c] >= 2 for c in trained),
                'characters_5_plus_artists': sum(artists_of[c] >= 5 for c in trained),
                'artists': len({r['artist'] for r in records})}
    return {'looks': looks, 'by_character': by_character, 'holdout': holdout, 'coverage': coverage}


def _usable(r: dict, character: str) -> Optional[dict]:
    """A reference record for `character` (a crop box when the image shows others too), or None."""
    if len(r['characters']) == 1:
        return {'path': r['path'], 'crop': None}
    box = r['boxes'].get(character)
    return {'path': r['path'], 'crop': box} if box else None


def iter_pairs(records: list[dict], plan: dict, seed: int = 0) -> Iterator[dict]:
    rng = random.Random(seed)
    looks, holdout = plan['looks'], plan['holdout']
    for character, group in plan['by_character'].items():
        if character in holdout or len(group) < 2:
            continue
        usable = [r for r in group if _usable(r, character)]
        by_artist = defaultdict(list)
        for r in usable:
            by_artist[r['artist']].append(r)
        for target in group:
            if target['characters'] != [character]:
                continue   # targets show only this character, so the prompt's other content stays coherent
            for kind, pool, accept in (
                    ('identity_style', by_artist[target['artist']], lambda r: r is not target),
                    ('identity', usable, lambda r: r['artist'] != target['artist'])):
                for r in _pick(rng, pool, accept, REFS_PER_TARGET):
                    yield {'kind': kind, 'character': character, 'target': target['path'], 'reference': _usable(r, character),
                           **_split(target, kind == 'identity_style', character, looks.get(character, []))}
    by_artist = defaultdict(list)
    for r in records:
        if not holdout.intersection(r['characters']):
            by_artist[r['artist']].append(r)
    for artist, group in by_artist.items():
        for target in group:
            own = set(target['characters'])
            for r in _pick(rng, group, lambda r: r is not target and not own.intersection(r['characters']), REFS_PER_TARGET):
                yield {'kind': 'style', 'character': None, 'target': target['path'],
                       'reference': {'path': r['path'], 'crop': None}, **_split(target, True, None, [])}


def build(records: list[dict], seed: int = 0) -> dict:
    """Everything in memory: for tests and small sets (export() streams instead)."""
    plan = _plan(records)
    pairs = list(iter_pairs(records, plan, seed))
    return {'pairs': pairs, 'eval': _evaluation(plan), 'looks': plan['looks'], 'coverage': plan['coverage'],
            'counts': dict(Counter(p['kind'] for p in pairs)), 'held_out': sorted(plan['holdout'])}


def _evaluation(plan: dict) -> list[dict]:
    return [{'character': c, 'images': [r['path'] for r in plan['by_character'][c]], 'look': plan['looks'].get(c, [])}
            for c in sorted(plan['holdout'])]


def export(group_ids: Optional[list[int]] = None, folder_ids: Optional[list[int]] = None, name: str = 'reference-pairs',
           accepted_only: bool = False, progress: Optional[Callable[[str], None]] = None) -> dict:
    from app.services.planner_store import planner_dir
    say = progress or (lambda message: None)
    folders = _folders(group_ids, folder_ids, accepted_only)
    say(f'Reading {len(folders):,} folders')
    records = collect(folders, lambda n: say(f'Read {n:,} images'))
    say(f'Pairing {len(records):,} images')
    plan = _plan(records)
    target = planner_dir() / 'exports' / name
    target.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with open(target / 'pairs.jsonl', 'w', encoding='utf-8') as fh:
        for pair in iter_pairs(records, plan):
            fh.write(json.dumps(pair, ensure_ascii=False) + '\n')
            counts[pair['kind']] += 1
            if sum(counts.values()) % 100_000 == 0:
                say(f'Wrote {sum(counts.values()):,} pairs')
    with open(target / 'eval.jsonl', 'w', encoding='utf-8') as fh:
        for item in _evaluation(plan):
            fh.write(json.dumps(item, ensure_ascii=False) + '\n')
    (target / 'looks.json').write_text(json.dumps(plan['looks'], ensure_ascii=False, indent=1), encoding='utf-8')
    summary = {'name': name, 'folders': len(folders), 'accepted_only': accepted_only, 'groups': _group_names(group_ids),
               'images': len(records), 'pairs': sum(counts.values()), 'by_kind': dict(counts), 'coverage': plan['coverage'],
               'held_out_characters': sorted(plan['holdout']), 'path': str(target),
               'created_at': datetime.now(timezone.utc).isoformat()}
    (target / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    (target / 'README.txt').write_text(
        'Reference pairs for a MOSS-Image reference adapter (Illustration Scrapping Studio).\n'
        'Paths are relative to the library images folder, like folders.csv in the training export.\n\n'
        '  pairs.jsonl  one pair per line: kind, character, target, reference {path, crop (0-1000 yxyx or null)},\n'
        '               keep (prompt tags), look (tags the reference supplies; 20% of prompts keep 30-70% of them),\n'
        '               minimal (10% of prompts)\n'
        '  eval.jsonl   held-out characters: their images and usual look, never in a pair\n'
        '  looks.json   every character\'s usual look\n\n'
        'coverage: characters outside the held-out set; *_2_plus_artists are the ones identity pairs can use.\n\n'
        + json.dumps(summary, indent=1) + '\n', encoding='utf-8')
    return summary


def _group_names(group_ids: Optional[list[int]]) -> Optional[list[str]]:
    """The exported groups' names (None: every group), so the history can tell exports of different scope apart."""
    if not group_ids:
        return None
    conn = db.get_connection()
    try:
        return [r[0] for r in conn.execute(f"SELECT name FROM artist_group WHERE id IN ({','.join('?' * len(group_ids))}) ORDER BY name", group_ids)]
    finally:
        conn.close()


def history() -> list[dict]:
    """Every pair export's summary, oldest first. Exports made before summary.json existed are read from the JSON
    block at the end of their README."""
    from app.services.planner_store import planner_dir
    root = planner_dir() / 'exports'
    items = []
    for folder in (root.iterdir() if root.is_dir() else []):
        if not (folder / 'pairs.jsonl').is_file():
            continue   # a training export, not pairs
        try:
            if (folder / 'summary.json').is_file():
                summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
            else:
                text = (folder / 'README.txt').read_text(encoding='utf-8')
                summary = json.loads(text[text.index('\n{') + 1:])
        except (OSError, ValueError):
            continue
        summary.setdefault('name', folder.name)
        summary.setdefault('groups', None)
        summary['held_out'] = len(summary.pop('held_out_characters', []) or [])
        items.append(summary)
    return sorted(items, key=lambda s: s.get('created_at') or '')


def status() -> dict:
    with _lock:
        return dict(_state)


def start(group_ids: Optional[list[int]] = None, folder_ids: Optional[list[int]] = None, name: str = 'reference-pairs',
          accepted_only: bool = False) -> dict:
    with _lock:
        if _state.get('status') == 'running':
            raise RuntimeError('Reference pairs are already being exported')
        _state.clear()
        _state.update(status='running', started_at=datetime.now(timezone.utc).isoformat(), progress='Starting')

    def say(message: str) -> None:
        with _lock:
            _state['progress'] = message

    def work():
        try:
            result = export(group_ids, folder_ids, name, accepted_only, say)
            with _lock:
                _state.update(status='done', result=result, finished_at=datetime.now(timezone.utc).isoformat())
                _state.pop('progress', None)
        except Exception as exc:
            with _lock:
                _state.update(status='error', error=f'{type(exc).__name__}: {exc}')

    threading.Thread(target=work, name='reference-pairs', daemon=True).start()
    return status()
