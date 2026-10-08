"""Character facts for captioning: who each character tag names, from which series, and how they usually look.

Captioning models are told which characters an image shows by its tags, but they often do not know rarer
characters or misread Danbooru's naming conventions. Instead of asking the model to search the web, this
builds the facts from the harvested metadata (millions of posts):

- name: the character tag without its qualifiers ("takanashi hoshino (blue archive)" -> "Takanashi Hoshino")
- series: the copyrights the character appears with in at least a quarter of its posts
- appearance: general tags (hair, eyes, ears, tails, horns, halos, species, fur ...) present in at least 40%
  of the posts showing that character alone, so other characters' traits do not leak in

The result is `<planner>/exports/character-facts.json`, keyed by tag family (danbooru, e621) and by the tag in
the trainer's space-separated spelling, the same spelling the `.txt` sidecars use. The captioning script
looks up the character tags it finds in each sidecar.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from app.services.planner_store import connect, now, planner_dir

MIN_POSTS = 3              # characters with fewer harvested posts are left out
SERIES_SHARE = 0.25        # a copyright on at least this share of the character's posts is one of its series
APPEARANCE_SHARE = 0.4     # an appearance tag on at least this share of solo posts is part of the usual look
MIN_SOLO_POSTS = 5         # fewer solo posts than this: no appearance facts
MAX_APPEARANCE = 12

# Tags that describe how a character looks, as opposed to the pose, outfit or scene of one picture.
_APPEARANCE_SUFFIXES = ('_hair', '_eyes', '_ears', '_tail', '_tails', '_horns', '_horn', '_wings', '_skin', '_fur', '_body',
                        '_scales', '_feathers', '_markings', '_pupils', '_sclera', '_halo', '_eyebrows', '_eyelashes',
                        '_mane', '_nose', '_muzzle', '_paws', '_stripes', '_spots')
_APPEARANCE_PREFIXES = ('hair_', 'two-tone_', 'multicolored_', 'streaked_', 'gradient_', 'colored_inner_')
_APPEARANCE_WORDS = {
    'ahoge', 'halo', 'horns', 'wings', 'tail', 'twintails', 'ponytail', 'side_ponytail', 'high_ponytail', 'low_ponytail',
    'braid', 'twin_braids', 'single_braid', 'french_braid', 'crown_braid', 'bangs', 'blunt_bangs', 'swept_bangs',
    'parted_bangs', 'sidelocks', 'hime_cut', 'bob_cut', 'drill_hair', 'twin_drills', 'heterochromia', 'glasses',
    'eyepatch', 'fang', 'fangs', 'skin_fang', 'freckles', 'mole', 'mole_under_eye', 'mole_under_mouth', 'scar',
    'scar_on_face', 'tattoo', 'facial_mark', 'pointy_ears', 'animal_ears', 'cat_ears', 'fox_ears', 'dog_ears',
    'wolf_ears', 'rabbit_ears', 'horse_ears', 'mouse_ears', 'bear_ears', 'animal_ear_fluff', 'cat_tail', 'fox_tail',
    'dog_tail', 'wolf_tail', 'rabbit_tail', 'demon_horns', 'dragon_horns', 'oni_horns', 'demon_tail', 'dragon_tail',
    'demon_wings', 'dragon_wings', 'angel_wings', 'feathered_wings', 'mechanical_halo', 'antenna_hair', 'dark_skin',
    'dark-skinned_female', 'dark-skinned_male', 'pale_skin', 'tan', 'muscular', 'muscular_female', 'muscular_male',
    'robot', 'android', 'mecha_musume', 'furry', 'anthro', 'feral', 'kemonomimi_mode', 'elf', 'slit_pupils',
    'tail_ornament', 'hair_ornament', 'hairclip', 'hair_bow', 'hair_ribbon', 'hairband', 'hair_flower', 'x_hair_ornament',
}
_NOT_APPEARANCE = {'hair_between_eyes', 'hair_over_one_eye', 'hair_over_eyes', 'hair_over_shoulder', 'hair_down', 'hair_up',
                   'hair_tucking', 'hair_pulled_back', 'hand_in_own_hair', 'holding_own_hair', 'hair_blowing', 'wet_hair',
                   'messy_hair', 'closed_eyes', 'half-closed_eyes', 'empty_eyes', 'rolling_eyes', 'wide_eyes', 'tears_in_eyes',
                   'covering_eyes', 'one_eye_closed', 'hair_flowing_over', 'blush_stickers'}

_lock = threading.Lock()
_state: dict = {'status': 'idle'}


def output_path() -> Path:
    return planner_dir() / 'exports' / 'character-facts.json'


def _spell(tag: str) -> str:
    """The trainer's spelling: underscores become spaces (the sidecar format)."""
    return ' '.join(tag.replace('_', ' ').split())


def is_appearance(tag: str) -> bool:
    if tag in _NOT_APPEARANCE:
        return False
    return tag in _APPEARANCE_WORDS or tag.endswith(_APPEARANCE_SUFFIXES) or tag.startswith(_APPEARANCE_PREFIXES)


def character_name(tag: str) -> tuple[str, list[str]]:
    """'hoshino_(swimsuit)_(blue_archive)' -> ('Hoshino', ['swimsuit', 'blue archive'])."""
    spelled = _spell(tag)
    qualifiers = re.findall(r'\(([^()]*)\)', spelled)
    base = re.sub(r'\s*\([^()]*\)', '', spelled).strip() or spelled
    name = ' '.join(word[:1].upper() + word[1:] for word in base.split())
    return name, [q.strip() for q in qualifiers if q.strip()]


def build(progress=lambda **_: None) -> dict:
    """One pass over every harvested post; writes and returns the facts file summary."""
    conn = connect()
    try:
        total = conn.execute('SELECT count(*) FROM post').fetchone()[0]
        posts = Counter()
        solo = Counter()
        series = defaultdict(Counter)
        looks = defaultdict(Counter)
        appearance_cache: dict[str, bool] = {}
        done = 0
        for row in conn.execute("SELECT site, characters, copyrights, general, species FROM post WHERE characters IS NOT NULL AND characters != ''"):
            done += 1
            if done % 200_000 == 0:
                progress(done=done, total=total)
            family = 'e621' if row['site'] == 'e621' else 'danbooru'
            characters = row['characters'].split()
            copyrights = (row['copyrights'] or '').split()
            for character in characters:
                key = (family, character)
                posts[key] += 1
                series[key].update(copyrights)
            if len(characters) == 1:
                key = (family, characters[0])
                solo[key] += 1
                # e621 species (fox, canine ...) are part of the look; general tags only when they describe it.
                traits = (row['species'] or '').split() if family == 'e621' else []
                for tag in (row['general'] or '').split():
                    flag = appearance_cache.get(tag)
                    if flag is None:
                        flag = appearance_cache[tag] = is_appearance(tag)
                    if flag:
                        traits.append(tag)
                looks[key].update(traits)
    finally:
        conn.close()
    facts: dict[str, dict] = {'danbooru': {}, 'e621': {}}
    for (family, character), count in posts.items():
        if count < MIN_POSTS:
            continue
        name, qualifiers = character_name(character)
        known_series = [_spell(tag) for tag, n in series[(family, character)].most_common(3) if n >= SERIES_SHARE * count]
        solo_count = solo[(family, character)]
        appearance = []
        if solo_count >= MIN_SOLO_POSTS:
            appearance = [_spell(tag) for tag, n in looks[(family, character)].most_common()
                          if n >= APPEARANCE_SHARE * solo_count][:MAX_APPEARANCE]
        entry = {'name': name, 'posts': count}
        if qualifiers:
            entry['qualifiers'] = qualifiers
        if known_series:
            entry['series'] = known_series
        if appearance:
            entry['appearance'] = appearance
        facts[family][_spell(character)] = entry
    path = output_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {'built_at': now(), 'posts_scanned': done, 'thresholds': {
        'min_posts': MIN_POSTS, 'series_share': SERIES_SHARE, 'appearance_share': APPEARANCE_SHARE, 'min_solo_posts': MIN_SOLO_POSTS},
        'characters': facts}
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temp.replace(path)
    return {'path': str(path), 'built_at': document['built_at'], 'posts_scanned': done,
            'characters': {family: len(entries) for family, entries in facts.items()},
            'with_series': sum(1 for entries in facts.values() for e in entries.values() if 'series' in e),
            'with_appearance': sum(1 for entries in facts.values() for e in entries.values() if 'appearance' in e),
            'bytes': path.stat().st_size}


def status() -> dict:
    with _lock:
        state = dict(_state)
    if state.get('status') in (None, 'idle') and output_path().exists():
        try:
            document = json.loads(output_path().read_text(encoding='utf-8'))
            state = {'status': 'idle', 'result': {'path': str(output_path()), 'built_at': document.get('built_at'),
                                                  'posts_scanned': document.get('posts_scanned'),
                                                  'characters': {k: len(v) for k, v in document.get('characters', {}).items()},
                                                  'bytes': output_path().stat().st_size}}
        except (OSError, ValueError):
            pass
    return state


def start() -> dict:
    with _lock:
        if _state.get('status') == 'running':
            raise RuntimeError('Character facts are already being built')
        _state.clear()
        _state.update(status='running', started_at=now(), done=0, total=None)

    def progress(done: int, total: Optional[int]):
        with _lock:
            _state.update(done=done, total=total)

    def work():
        try:
            result = build(progress)
            with _lock:
                _state.update(status='completed', result=result, finished_at=now())
        except Exception as exc:  # reported in the panel
            with _lock:
                _state.update(status='failed', error=f'{type(exc).__name__}: {exc}', finished_at=now())

    threading.Thread(target=work, daemon=True, name='caption-facts').start()
    return status()
