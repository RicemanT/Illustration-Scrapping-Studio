"""Caption check: rule-based quality control for the natural-language captions of planner collections.

The captioning prompt's rules are mostly mechanical, so code verifies them (no model, no content policy):
the caption starts with the artist trigger ("Drawn by ...") exactly once, stays within the word range in one
paragraph, avoids "or" hedging, markup and "This image shows" phrasing, names every character the tags
name (using the character facts' clean names), never leaks a raw tag qualifier, quotes some text when the
tags say the image has text, and is not a refusal or an echo of the prompt.

Flagged captions can be set aside (renamed to `<caption>.flagged`) so the captioning script, which skips
images that already have a caption, writes them again on its next run.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Optional

import app.db as db
from app.services import caption_facts
from app.services.captions import caption_file, get_suffix
from app.services.planner_store import connect as planner_connect, now, planner_dir

CHECKS = {
    'refusal': 'Refusal or safety message',
    'echo': 'Repeats the prompt (<tags>, <characters>)',
    'artist_start': "Does not start with the artist trigger",
    'double_drawn_by': '"Drawn by" twice',
    'too_short': 'Shorter than the minimum words',
    'too_long': 'Longer than the maximum words',
    'line_break': 'More than one paragraph',
    'hedging': 'Uses "or" outside quoted text',
    'markup': 'Asterisks or square brackets',
    'meta_phrase': '"This image shows" style phrasing',
    'missing_character': 'A tagged character is not named',
    'raw_tag': 'Raw tag spelling (qualifier in parentheses or underscores)',
    'missing_text': 'Tags mention text but nothing is quoted',
}
TEXT_TAGS = {'text', 'english text', 'japanese text', 'korean text', 'chinese text', 'speech bubble', 'watermark', 'signature',
             'artist name', 'web address', 'dated', 'logo', 'sound effects', 'character name', 'copyright name', 'twitter username'}
QUOTED = re.compile(r'"[^"]*"|“[^”]*”|「[^」]*」')
META = re.compile(r'^\s*(?:this|the)\s+(?:image|picture|illustration|artwork)\s+(?:shows|displays|depicts|features|presents)'
                  r'|\byou are looking at\b|\bin this (?:image|picture|illustration)\b', re.IGNORECASE)
HEDGE = re.compile(r'\bor\b', re.IGNORECASE)
REFUSAL = re.compile(r"\bi(?:'?m|\s+am)?\s+(?:unable|sorry)\b|\bi\s+(?:can'?t|cannot|won'?t|will not)\s+(?:help|assist|describe|provide|create|caption)"
                     r"|\bas an ai\b|\bcontent policy\b|\bsafety guidelines\b|\bi must decline\b|\bi apologi[sz]e\b", re.IGNORECASE)

_lock = threading.Lock()
_state: dict = {'status': 'idle'}


def report_path() -> Path:
    return planner_dir() / 'exports' / 'caption-check.json'


def _facts() -> dict:
    path = caption_facts.output_path()
    if not path.exists():
        return {}
    characters = json.loads(path.read_text(encoding='utf-8')).get('characters', {})
    merged = dict(characters.get('e621', {}))
    merged.update(characters.get('danbooru', {}))
    return merged


def _name_words(name: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[\w'-]+", name) if len(word) >= 3]


def check_caption(caption: str, tags: list[str], facts: dict, min_words: int, max_words: int) -> list[str]:
    """The problems of one caption (codes from CHECKS)."""
    problems = []
    text = caption.strip()
    unquoted = QUOTED.sub(' ', text)
    lowered = text.lower()
    if REFUSAL.search(unquoted):
        problems.append('refusal')
    if '<tags>' in lowered or '</tags>' in lowered or '<characters>' in lowered:
        problems.append('echo')
    first = tags[0] if tags else ''
    if first.lower().startswith('drawn by'):
        if not lowered.startswith(first.lower()):
            problems.append('artist_start')
    if re.match(r'\s*drawn by\s+drawn by\b', lowered) or lowered.count('drawn by') > 1:
        problems.append('double_drawn_by')
    words = len(text.split())
    if words < min_words:
        problems.append('too_short')
    if words > max_words:
        problems.append('too_long')
    if '\n' in text:
        problems.append('line_break')
    if HEDGE.search(unquoted):
        problems.append('hedging')
    if re.search(r'[*\[\]]', unquoted):
        problems.append('markup')
    if META.search(unquoted):
        problems.append('meta_phrase')
    caption_words = set(re.findall(r"[\w'-]+", lowered))
    for tag in tags[1:]:
        fact = facts.get(tag.lower())
        if fact and not any(word in caption_words for word in _name_words(fact['name'])):
            problems.append('missing_character')
            break
    if '_' in unquoted or any(f'({q.lower()})' in lowered for tag in tags if (fact := facts.get(tag.lower())) for q in fact.get('qualifiers', [])):
        problems.append('raw_tag')
    if TEXT_TAGS.intersection(t.lower() for t in tags) and not QUOTED.search(text):
        problems.append('missing_text')
    return problems


def _planner_images() -> list[dict]:
    planner = planner_connect()
    try:
        folders = {r[0] for r in planner.execute('SELECT DISTINCT folder_id FROM delivery_item WHERE folder_id IS NOT NULL')}
    finally:
        planner.close()
    if not folders:
        return []
    main = db.get_connection()
    try:
        marks = ','.join('?' * len(folders))
        return [dict(r) for r in main.execute(
            f"""SELECT i.id, i.path, i.folder_id, c.name AS folder FROM image i JOIN collection c ON c.id=i.folder_id
                WHERE i.folder_id IN ({marks}) ORDER BY i.folder_id, i.id""", tuple(folders))]
    finally:
        main.close()


def run(min_words: int = 200, max_words: int = 350, progress=lambda **_: None) -> dict:
    root = (db.LIBRARY_PATH / 'images').resolve()
    suffix = get_suffix()
    facts = _facts()
    images = _planner_images()
    counts, flagged = Counter(), []
    captioned = 0
    for index, image in enumerate(images, 1):
        if index % 2000 == 0:
            progress(done=index, total=len(images))
        path = root / image['path']
        caption_path = caption_file(path, suffix)
        if not caption_path.exists():
            continue
        captioned += 1
        try:
            caption = caption_path.read_text(encoding='utf-8')
            sidecar = path.with_suffix('.txt')
            tags = [t.strip() for t in sidecar.read_text(encoding='utf-8').split(',') if t.strip()] if sidecar.exists() else []
        except OSError:
            continue
        problems = check_caption(caption, tags, facts, min_words, max_words)
        if problems:
            counts.update(problems)
            flagged.append({'image_id': image['id'], 'folder_id': image['folder_id'], 'folder': image['folder'], 'path': image['path'],
                            'problems': problems, 'words': len(caption.split()), 'start': caption.strip()[:120]})
    result = {'checked_at': now(), 'images': len(images), 'captioned': captioned, 'flagged': len(flagged),
              'counts': dict(counts.most_common()), 'labels': CHECKS, 'facts': bool(facts), 'suffix': suffix,
              'min_words': min_words, 'max_words': max_words}
    path = report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**result, 'items': flagged}, ensure_ascii=False), encoding='utf-8')
    return result


def flagged_items(problem: Optional[str] = None, limit: int = 300, folder_id: Optional[int] = None) -> list[dict]:
    path = report_path()
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding='utf-8')).get('items', [])
    if problem:
        items = [item for item in items if problem in item['problems']]
    if folder_id is not None:
        items = [item for item in items if item['folder_id'] == folder_id]
    return items[:limit]


def set_aside(problems: list[str]) -> dict:
    """Rename the flagged captions with any of `problems` to `<caption>.flagged`, so they get captioned again."""
    path = report_path()
    if not path.exists():
        raise LookupError('Run the caption check first')
    report = json.loads(path.read_text(encoding='utf-8'))
    root = (db.LIBRARY_PATH / 'images').resolve()
    suffix = report.get('suffix') or get_suffix()
    wanted = set(problems) or set(CHECKS)
    moved = 0
    for item in report.get('items', []):
        if not wanted.intersection(item['problems']):
            continue
        caption = caption_file(root / item['path'], suffix)
        if caption.exists() and root in caption.resolve().parents:
            caption.replace(caption.with_name(caption.name + '.flagged'))
            moved += 1
    return {'set_aside': moved}


def status() -> dict:
    with _lock:
        state = dict(_state)
    if state.get('status') == 'idle' and report_path().exists():
        try:
            report = json.loads(report_path().read_text(encoding='utf-8'))
            report.pop('items', None)
            state['result'] = report
        except (OSError, ValueError):
            pass
    return state


def start(min_words: int, max_words: int) -> dict:
    with _lock:
        if _state.get('status') == 'running':
            raise RuntimeError('A caption check is already running')
        _state.clear()
        _state.update(status='running', started_at=now(), done=0, total=None)

    def progress(done: int, total: int):
        with _lock:
            _state.update(done=done, total=total)

    def work():
        try:
            result = run(min_words, max_words, progress)
            with _lock:
                _state.update(status='completed', result=result, finished_at=now())
        except Exception as exc:
            with _lock:
                _state.update(status='failed', error=f'{type(exc).__name__}: {exc}', finished_at=now())

    threading.Thread(target=work, daemon=True, name='caption-check').start()
    return status()
