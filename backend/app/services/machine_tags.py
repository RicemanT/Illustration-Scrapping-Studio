"""Machine tags: general tags from the tagger notebooks added to the trainer tags.

The tagger notebooks (DBv4 for Danbooru/Gelbooru images, Hydra for e621 images) never touch the `.txt` sidecars,
which the app rebuilds from its database on every tag edit, mark or review. They write JSON lines to
`<planner>/tagger/*.jsonl`, one line per image:

    {"image_id": 12, "sha256": "...", "path": "group/artist/abc.jpg", "model": "dbv4-full",
     "tags": [["long hair", 0.93], ["smile", 0.71]]}

Importing stores them in `image_machine_tag`, apart from the provider tag rows (delivery, tracker and dedup read
those as booru posts). The sidecar builder appends them after the booru general tags: a tag the post already has is
not repeated, and a machine tag removed in the tag editor stays removed (an ordinary remove override).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import app.db as db
from app.services.planner_store import now, planner_dir
from app.services.tags import TagService, normalize_tag

_lock = threading.Lock()
_state: dict = {'status': 'idle'}
COMMIT_EVERY = 2000


def tagger_dir() -> Path:
    return planner_dir() / 'tagger'


def _state_file() -> Path:
    return tagger_dir() / '.imported.json'


def _files() -> list[Path]:
    folder = tagger_dir()
    return sorted(folder.glob('*.jsonl')) if folder.is_dir() else []


def _signature(path: Path) -> dict:
    stat = path.stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def summary() -> dict:
    """Machine tags in the database per model, and the result files waiting in the tagger folder."""
    conn = db.get_connection()
    try:
        models = {row[0]: {'images': row[1], 'tags': row[2]} for row in conn.execute(
            'SELECT model, COUNT(DISTINCT image_id), COUNT(*) FROM image_machine_tag GROUP BY model ORDER BY model')}
    finally:
        conn.close()
    try:
        imported = json.loads(_state_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        imported = {}
    files = [{'name': path.name, 'bytes': path.stat().st_size, 'new': imported.get(path.name) != _signature(path)} for path in _files()]
    return {'folder': str(tagger_dir()), 'models': models, 'files': files}


def _clean(tags) -> dict[str, float]:
    result: dict[str, Optional[float]] = {}
    seen: set[str] = set()
    for item in tags or []:
        tag, confidence = (item, None) if isinstance(item, str) else (item[0], item[1] if len(item) > 1 else None)
        tag = normalize_tag(tag)
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            result[tag] = round(float(confidence), 4) if confidence is not None else None
    return result


def run_import(everything: bool = False, progress=lambda **_: None) -> dict:
    """Read new or changed result files into image_machine_tag, then rebuild the sidecars that changed."""
    folder = tagger_dir()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        imported = {} if everything else json.loads(_state_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        imported = {}
    files = [path for path in _files() if everything or imported.get(path.name) != _signature(path)]
    counts = {'files': len(files), 'lines': 0, 'images_updated': 0, 'unchanged': 0, 'unknown_images': 0, 'bad_lines': 0}
    touched: set[int] = set()
    conn = db.get_connection()
    try:
        by_path = {row[1].replace('\\', '/'): row[0] for row in conn.execute('SELECT id, path FROM image WHERE folder_id IS NOT NULL')}
        known = {row[0]: (row[1].replace('\\', '/'), row[2]) for row in conn.execute('SELECT id, path, sha256 FROM image WHERE folder_id IS NOT NULL')}
        pending = 0
        for path in files:
            signature = _signature(path)
            with path.open(encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    counts['lines'] += 1
                    try:
                        record = json.loads(line)
                        model = str(record['model']).strip()
                        tags = _clean(record.get('tags'))
                    except (ValueError, KeyError, TypeError, IndexError):
                        counts['bad_lines'] += 1
                        continue
                    # The image id is stable when a folder is renamed or merged (the path changes); the path or the
                    # file hash confirms it is still the same picture.
                    image_id = record.get('image_id')
                    entry = known.get(image_id)
                    record_path = str(record.get('path') or '').replace('\\', '/')
                    if not (entry and model and (entry[0] == record_path or (record.get('sha256') and entry[1] == record.get('sha256')))):
                        image_id = by_path.get(record_path)
                    if image_id is None or not model:
                        counts['unknown_images'] += 1
                        continue
                    existing = {row[0]: row[1] for row in conn.execute(
                        'SELECT tag, confidence FROM image_machine_tag WHERE image_id=? AND model=?', (image_id, model))}
                    if {k.casefold() for k in existing} == {k.casefold() for k in tags}:
                        counts['unchanged'] += 1
                        continue
                    conn.execute('DELETE FROM image_machine_tag WHERE image_id=? AND model=?', (image_id, model))
                    stamp = now()
                    conn.executemany('INSERT INTO image_machine_tag (image_id, model, tag, confidence, added_at) VALUES (?, ?, ?, ?, ?)',
                                     [(image_id, model, tag, confidence, stamp) for tag, confidence in tags.items()])
                    touched.add(image_id)
                    counts['images_updated'] += 1
                    pending += 1
                    if pending >= COMMIT_EVERY:
                        conn.commit()
                        pending = 0
                        progress(stage='reading', done=counts['lines'], total=None)
            conn.commit()
            imported[path.name] = signature
            _state_file().write_text(json.dumps(imported), encoding='utf-8')
    finally:
        conn.close()
    counts['sidecars_rewritten'], counts['warnings'] = _rewrite(touched, progress)
    return counts


def clear(model: str, progress=lambda **_: None) -> dict:
    """Remove one model's machine tags and rebuild the affected sidecars (its result files may be imported again)."""
    conn = db.get_connection()
    try:
        image_ids = {row[0] for row in conn.execute('SELECT DISTINCT image_id FROM image_machine_tag WHERE model=?', (model,))}
        conn.execute('DELETE FROM image_machine_tag WHERE model=?', (model,))
        conn.commit()
    finally:
        conn.close()
    try:
        _state_file().unlink(missing_ok=True)
    except OSError:
        pass
    rewritten, warnings = _rewrite(image_ids, progress)
    return {'model': model, 'images': len(image_ids), 'sidecars_rewritten': rewritten, 'warnings': warnings}


def _rewrite(image_ids, progress) -> tuple[int, list[str]]:
    service = TagService(db.LIBRARY_PATH)
    warnings: list[str] = []
    ids = sorted(image_ids)
    for index, image_id in enumerate(ids, 1):
        warning = service._rewrite_sidecar(image_id)
        if warning:
            warnings.append(warning)
        if index % 500 == 0:
            progress(stage='writing sidecars', done=index, total=len(ids))
    return len(ids) - len(warnings), warnings[:20]


def status() -> dict:
    with _lock:
        state = dict(_state)
    state['summary'] = summary()
    return state


def start(action: str = 'import', everything: bool = False, model: Optional[str] = None) -> dict:
    with _lock:
        if _state.get('status') == 'running':
            raise RuntimeError('A machine tag job is already running')
        _state.clear()
        _state.update(status='running', action=action, started_at=now(), stage='reading', done=0, total=None)

    def progress(**fields):
        with _lock:
            _state.update(fields)

    def work():
        try:
            result = clear(model, progress) if action == 'clear' else run_import(everything, progress)
            with _lock:
                _state.update(status='completed', result=result, finished_at=now())
        except Exception as exc:  # reported in the panel
            with _lock:
                _state.update(status='failed', error=f'{type(exc).__name__}: {exc}', finished_at=now())

    threading.Thread(target=work, daemon=True, name='machine-tags').start()
    return status()
