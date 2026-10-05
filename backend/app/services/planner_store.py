"""Dataset Planner storage, imports, plan runs and manifest export.

The planner keeps its own SQLite file (`<library>/planner/planner.db`) because
harvested post metadata can reach millions of rows. Nothing here touches the
main library database, collections or image files.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import app.db as db
from app.services.planner_select import (
    MOTION_EXTS, PlannerConfig, RarityIndex, candidate_from_row, plan_family, real_artists, rejection, score_artist,
)

SITES = ('danbooru', 'gelbooru', 'e621')
FAMILY = {'danbooru': 'danbooru', 'gelbooru': 'danbooru', 'e621': 'e621'}
POST_COLUMNS = ('site', 'remote_id', 'artist_id', 'md5', 'width', 'height', 'ext', 'rating', 'score', 'fav_count',
                'parent_id', 'has_children', 'created_at', 'artists', 'characters', 'copyrights', 'species',
                'general', 'meta', 'file_url', 'preview_url')
_run_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS artist (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL,
    tag TEXT NOT NULL,
    display_name TEXT NOT NULL,
    tag_id TEXT,
    listed_post_count INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    harvest_status TEXT NOT NULL DEFAULT 'pending',
    harvest_cursor TEXT,
    harvested_posts INTEGER NOT NULL DEFAULT 0,
    harvested_at TEXT,
    harvest_error TEXT,
    UNIQUE (site, tag)
);
CREATE TABLE IF NOT EXISTS post (
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
    md5 TEXT, width INTEGER, height INTEGER, ext TEXT, rating TEXT,
    score INTEGER, fav_count INTEGER, parent_id TEXT, has_children INTEGER, created_at TEXT,
    artists TEXT, characters TEXT, copyrights TEXT, species TEXT, general TEXT, meta TEXT,
    file_url TEXT, preview_url TEXT,
    PRIMARY KEY (artist_id, site, remote_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS character_target (
    family TEXT NOT NULL,
    tag TEXT NOT NULL,
    post_count INTEGER,
    rank INTEGER NOT NULL,
    source TEXT,
    PRIMARY KEY (family, tag)
);
CREATE TABLE IF NOT EXISTS override (
    artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('lock', 'ban')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, site, remote_id)
);
CREATE TABLE IF NOT EXISTS harvest_job (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    parameters TEXT NOT NULL,
    progress TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    config TEXT NOT NULL,
    summary TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS run_artist (
    run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    usable INTEGER NOT NULL,
    selected INTEGER NOT NULL,
    repeats INTEGER NOT NULL,
    PRIMARY KEY (run_id, artist_id)
);
CREATE TABLE IF NOT EXISTS delivery (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES run(id),
    status TEXT NOT NULL,
    group_prefix TEXT NOT NULL,
    progress TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS delivery_item (
    delivery_id INTEGER NOT NULL REFERENCES delivery(id) ON DELETE CASCADE,
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    artist_id INTEGER NOT NULL,
    folder_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    images INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    PRIMARY KEY (delivery_id, site, remote_id, artist_id)
);
CREATE INDEX IF NOT EXISTS delivery_item_pending ON delivery_item(delivery_id, site, status);
CREATE TABLE IF NOT EXISTS style_flag (
    artist_id INTEGER NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    distance REAL NOT NULL,
    score REAL NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (artist_id, site, remote_id)
);
CREATE TABLE IF NOT EXISTS selection (
    run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    role TEXT NOT NULL,
    pick_order INTEGER NOT NULL,
    repeats INTEGER NOT NULL,
    gain REAL NOT NULL,
    reasons TEXT NOT NULL,
    PRIMARY KEY (run_id, artist_id, site, remote_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def planner_dir() -> Path:
    override = os.getenv('ARTIST_PLANNER_PATH')
    return Path(override).expanduser().resolve() if override else db.LIBRARY_PATH / 'planner'


def connect() -> sqlite3.Connection:
    path = planner_dir() / 'planner.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.executescript(SCHEMA)
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(character_target)')}
    if 'priority' not in columns:
        conn.execute('ALTER TABLE character_target ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
        conn.commit()
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(artist)')}
    if 'in_list' not in columns:
        # in_list: on the latest artists CSV. enabled: also part of the current plan scope.
        conn.execute('ALTER TABLE artist ADD COLUMN in_list INTEGER NOT NULL DEFAULT 1')
        conn.execute('UPDATE artist SET in_list=enabled')
        conn.commit()
    return conn


def recover() -> None:
    """Jobs cannot survive a backend restart; mark them so the UI offers a resume."""
    conn = connect()
    try:
        conn.execute("UPDATE harvest_job SET status='interrupted', finished_at=? WHERE status IN ('queued','running','cancelling')", (now(),))
        conn.execute("UPDATE artist SET harvest_status='pending' WHERE harvest_status='running'")
        conn.execute("UPDATE run SET status='interrupted', finished_at=? WHERE status='running'", (now(),))
        conn.execute("UPDATE delivery SET status='interrupted', finished_at=? WHERE status IN ('queued','running','cancelling')", (now(),))
        conn.commit()
    finally:
        conn.close()


def _csv_rows(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿')))
    return [{(k or '').strip(): (v or '').strip() for k, v in row.items()} for row in reader]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def import_artists(text: str, disable_missing: bool = True) -> dict:
    """Upsert artists.csv rows (site, display_name, query_tag, tag_id, post_count).

    Artists missing from a replacement list are disabled, not deleted, so their
    harvested metadata survives an accidental edit.
    """
    rows = _csv_rows(text)
    errors, parsed = [], {}
    for line, row in enumerate(rows, start=2):
        site = row.get('site', '').lower()
        tag = row.get('query_tag', '').replace(' ', '_')
        if site not in SITES or not tag:
            errors.append(f'line {line}: needs a site in {", ".join(SITES)} and a query_tag')
            continue
        parsed[(site, tag)] = (row.get('display_name') or tag.replace('_', ' '), row.get('tag_id') or None, _int(row.get('post_count')))
    if errors:
        return {'imported': 0, 'errors': errors[:50], 'error_count': len(errors)}
    conn = connect()
    try:
        existing = {(r['site'], r['tag']): r['id'] for r in conn.execute('SELECT id, site, tag FROM artist')}
        added = updated = 0
        for (site, tag), (name, tag_id, count) in parsed.items():
            if (site, tag) in existing:
                conn.execute('UPDATE artist SET display_name=?, tag_id=?, listed_post_count=?, enabled=1, in_list=1 WHERE id=?',
                             (name, tag_id, count, existing[(site, tag)]))
                updated += 1
            else:
                conn.execute('INSERT INTO artist (site, tag, display_name, tag_id, listed_post_count) VALUES (?,?,?,?,?)',
                             (site, tag, name, tag_id, count))
                added += 1
        disabled = 0
        if disable_missing:
            missing = [artist_id for key, artist_id in existing.items() if key not in parsed]
            for artist_id in missing:
                disabled += conn.execute('UPDATE artist SET enabled=0, in_list=0 WHERE id=? AND in_list=1', (artist_id,)).rowcount
        conn.commit()
        return {'imported': len(parsed), 'added': added, 'updated': updated, 'disabled': disabled, 'errors': []}
    finally:
        conn.close()


def enable_only(lines: list[str]) -> dict:
    """Limit planning to the listed artists (for a pilot); the rest stay listed but disabled.

    Each line is a tag or display name, optionally prefixed with a site
    ("e621,some_artist" or "e621:some_artist"). Unknown lines change nothing.
    """
    conn = connect()
    try:
        artists = conn.execute('SELECT id, site, tag, display_name FROM artist WHERE in_list=1').fetchall()
        by_name = defaultdict(list)
        for a in artists:
            for key in {a['tag'].casefold(), a['tag'].replace('_', ' ').casefold(), a['display_name'].casefold()}:
                by_name[key].append(a)
        chosen, unknown, ambiguous = set(), [], []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            site = None
            for separator in (',', ':'):
                head, _, rest = line.partition(separator)
                if rest and head.strip().lower() in SITES:
                    site, line = head.strip().lower(), rest.strip()
                    break
            matches = [a for a in by_name.get(line.casefold(), []) if site is None or a['site'] == site]
            if not matches:
                unknown.append(raw.strip())
            elif len({a['id'] for a in matches}) > 1 and site is None:
                ambiguous.append(raw.strip())
                chosen.update(a['id'] for a in matches)
            else:
                chosen.update(a['id'] for a in matches)
        if not chosen:
            return {'enabled': 0, 'unknown': unknown, 'ambiguous': ambiguous}
        conn.execute('UPDATE artist SET enabled=0 WHERE in_list=1')
        conn.executemany('UPDATE artist SET enabled=1 WHERE id=?', [(i,) for i in chosen])
        conn.commit()
        return {'enabled': len(chosen), 'unknown': unknown, 'ambiguous': ambiguous}
    finally:
        conn.close()


def enable_all() -> int:
    """Enable every artist on the latest artists CSV again."""
    conn = connect()
    try:
        changed = conn.execute('UPDATE artist SET enabled=1 WHERE in_list=1 AND enabled=0').rowcount
        conn.commit()
        return changed
    finally:
        conn.close()


def import_characters(text: str) -> dict:
    """Replace character targets from characters.csv (site, tag, post_count, rank)."""
    rows = _csv_rows(text)
    targets, errors = {}, []
    for line, row in enumerate(rows, start=2):
        site = row.get('site', '').lower()
        tag = row.get('tag', '').replace(' ', '_')
        if site not in SITES or not tag:
            errors.append(f'line {line}: needs a site and a tag')
            continue
        key = (FAMILY[site], tag)
        if key not in targets:
            targets[key] = (_int(row.get('post_count')), len(targets) + 1, row.get('source') or None)
    if errors:
        return {'imported': 0, 'errors': errors[:50], 'error_count': len(errors)}
    conn = connect()
    try:
        conn.execute('DELETE FROM character_target')
        conn.executemany('INSERT INTO character_target (family, tag, post_count, rank, source) VALUES (?,?,?,?,?)',
                         [(family, tag, *values) for (family, tag), values in targets.items()])
        conn.commit()
        return {'imported': len(targets), 'by_family': dict(Counter(family for family, _ in targets)), 'errors': []}
    finally:
        conn.close()


def replace_family_characters(family: str, rows: list[dict], source: str) -> dict:
    """Replace one family's ranked targets, keeping the other family and priority targets."""
    conn = connect()
    try:
        priority = {r['tag'] for r in conn.execute('SELECT tag FROM character_target WHERE family=? AND priority=1', (family,))}
        conn.execute('DELETE FROM character_target WHERE family=? AND priority=0', (family,))
        conn.executemany('INSERT OR IGNORE INTO character_target (family, tag, post_count, rank, source) VALUES (?,?,?,?,?)',
                         [(family, row['tag'], row.get('post_count'), rank, source) for rank, row in enumerate(rows, start=1)
                          if row['tag'] not in priority])
        conn.commit()
        return {'family': family, 'imported': len(rows)}
    finally:
        conn.close()


def add_priority_characters(family: str, rows: list[dict], source: str) -> dict:
    """Mark characters as priority targets, adding any that are not targets yet."""
    conn = connect()
    try:
        next_rank = (conn.execute('SELECT max(rank) FROM character_target WHERE family=?', (family,)).fetchone()[0] or 0) + 1
        added = 0
        for row in rows:
            updated = conn.execute('UPDATE character_target SET priority=1 WHERE family=? AND tag=?', (family, row['tag'])).rowcount
            if not updated:
                conn.execute('INSERT INTO character_target (family, tag, post_count, rank, source, priority) VALUES (?,?,?,?,?,1)',
                             (family, row['tag'], row.get('post_count'), next_rank, source))
                next_rank += 1
                added += 1
        conn.commit()
        return {'family': family, 'priority': len(rows), 'added': added}
    finally:
        conn.close()


def clear_priority_characters() -> int:
    """Unmark every priority target; targets that were only priority are removed."""
    conn = connect()
    try:
        removed = conn.execute("DELETE FROM character_target WHERE priority=1 AND source LIKE 'series:%'").rowcount
        conn.execute('UPDATE character_target SET priority=0')
        conn.commit()
        return removed
    finally:
        conn.close()


def set_override(artist_id: int, site: str, remote_id: str, action: str) -> None:
    conn = connect()
    try:
        if not conn.execute('SELECT 1 FROM post WHERE artist_id=? AND site=? AND remote_id=?', (artist_id, site, remote_id)).fetchone():
            raise LookupError('Post not found for this artist')
        if action == 'clear':
            conn.execute('DELETE FROM override WHERE artist_id=? AND site=? AND remote_id=?', (artist_id, site, remote_id))
        else:
            conn.execute('INSERT OR REPLACE INTO override (artist_id, site, remote_id, action, created_at) VALUES (?,?,?,?,?)',
                         (artist_id, site, remote_id, action, now()))
        conn.commit()
    finally:
        conn.close()


def status() -> dict:
    conn = connect()
    try:
        artists = defaultdict(lambda: {'enabled': 0, 'disabled': 0, 'harvest': {}})
        for r in conn.execute('SELECT site, enabled, harvest_status, count(*) n FROM artist GROUP BY site, enabled, harvest_status'):
            entry = artists[r['site']]
            entry['enabled' if r['enabled'] else 'disabled'] += r['n']
            if r['enabled']:
                entry['harvest'][r['harvest_status']] = entry['harvest'].get(r['harvest_status'], 0) + r['n']
        # Counting post rows scans the whole (multi-GB) table; harvests keep per-artist totals.
        posts = {r['site']: r['n'] for r in conn.execute('SELECT site, sum(harvested_posts) n FROM artist GROUP BY site')}
        characters = {r['family']: r['n'] for r in conn.execute('SELECT family, count(*) n FROM character_target GROUP BY family')}
        priority_characters = {r['family']: r['n'] for r in conn.execute('SELECT family, count(*) n FROM character_target WHERE priority=1 GROUP BY family')}
        overrides = {r['action']: r['n'] for r in conn.execute('SELECT action, count(*) n FROM override GROUP BY action')}
        style_flags = conn.execute('SELECT count(*) FROM style_flag').fetchone()[0]
        listed = conn.execute('SELECT count(*) FROM artist WHERE in_list=1').fetchone()[0]
        job = conn.execute('SELECT * FROM harvest_job ORDER BY id DESC LIMIT 1').fetchone()
        delivery = conn.execute('SELECT id FROM delivery ORDER BY id DESC LIMIT 1').fetchone()
        runs = [run_row(r) for r in conn.execute('SELECT * FROM run ORDER BY id DESC LIMIT 10')]
        return {'artists': dict(artists), 'posts': posts, 'characters': characters, 'priority_characters': priority_characters,
                'overrides': overrides, 'style_flags': style_flags, 'listed_artists': listed,
                'delivery_id': delivery['id'] if delivery else None,
                'harvest_job': harvest_row(job) if job else None, 'runs': runs, 'path': str(planner_dir()),
                'library': str(db.LIBRARY_PATH), 'custom_path': planner_dir() != db.LIBRARY_PATH / 'planner'}
    finally:
        conn.close()


def harvest_row(row) -> dict:
    data = dict(row)
    data['parameters'] = json.loads(data['parameters'])
    data['progress'] = json.loads(data['progress'])
    return data


def run_row(row) -> dict:
    data = dict(row)
    data['config'] = json.loads(data['config'])
    data['summary'] = json.loads(data['summary']) if data['summary'] else None
    return data


def list_artists(site: str | None = None, query: str = '', run_id: int | None = None, run_status: str | None = None,
                 offset: int = 0, limit: int = 100, flagged: bool = False) -> dict:
    conn = connect()
    try:
        where, params = ['a.enabled=1'], []
        if flagged:
            where.append('EXISTS (SELECT 1 FROM style_flag f WHERE f.artist_id=a.id)')
        if site:
            where.append('a.site=?'); params.append(site)
        if query:
            where.append('(a.tag LIKE ? OR a.display_name LIKE ?)'); params += [f'%{query}%'] * 2
        join = 'LEFT JOIN run_artist ra ON ra.artist_id=a.id AND ra.run_id=?' if run_id else ''
        if run_id and run_status:
            where.append('ra.status=?'); params.append(run_status)
        sql_where = ' AND '.join(where)
        join_params = [run_id] if run_id else []
        total = conn.execute(f'SELECT count(*) FROM artist a {join} WHERE {sql_where}', join_params + params).fetchone()[0]
        columns = 'a.*, (SELECT count(*) FROM style_flag f WHERE f.artist_id=a.id) style_flags' + (
            ', ra.status run_status, ra.reason run_reason, ra.usable, ra.selected, ra.repeats' if run_id else '')
        rows = conn.execute(f'SELECT {columns} FROM artist a {join} WHERE {sql_where} ORDER BY a.site, a.display_name LIMIT ? OFFSET ?',
                            join_params + params + [limit, offset]).fetchall()
        return {'items': [dict(r) for r in rows], 'total': total}
    finally:
        conn.close()


def artist_detail(artist_id: int, run_id: int | None, runners_up: int = 40) -> dict:
    """Selected images for a run plus the best unselected posts, for review."""
    conn = connect()
    try:
        artist = conn.execute('SELECT * FROM artist WHERE id=?', (artist_id,)).fetchone()
        if not artist:
            raise LookupError('Artist not found')
        overrides = {(r['site'], r['remote_id']): r['action'] for r in conn.execute('SELECT * FROM override WHERE artist_id=?', (artist_id,))}
        flags = {(r['site'], r['remote_id']): {'distance': r['distance'], 'score': r['score']}
                 for r in conn.execute('SELECT * FROM style_flag WHERE artist_id=?', (artist_id,))}
        selected = []
        if run_id:
            for r in conn.execute("""SELECT s.*, p.preview_url, p.file_url, p.width, p.height, p.rating, p.characters, p.fav_count, p.score
                                     FROM selection s JOIN post p ON p.artist_id=s.artist_id AND p.site=s.site AND p.remote_id=s.remote_id
                                     WHERE s.run_id=? AND s.artist_id=? ORDER BY s.pick_order""", (run_id, artist_id)):
                item = dict(r)
                item['reasons'] = json.loads(item['reasons'])
                item['override'] = overrides.get((r['site'], r['remote_id']))
                item['style_flag'] = flags.get((r['site'], r['remote_id']))
                selected.append(item)
        chosen = {(s['site'], s['remote_id']) for s in selected}
        others = []
        for r in conn.execute("""SELECT site, remote_id, preview_url, file_url, width, height, rating, characters, fav_count, score
                                 FROM post WHERE artist_id=? ORDER BY COALESCE(fav_count, score, 0) DESC, remote_id LIMIT ?""",
                              (artist_id, len(chosen) + runners_up)):
            if (r['site'], r['remote_id']) not in chosen and len(others) < runners_up:
                others.append({**dict(r), 'override': overrides.get((r['site'], r['remote_id']))})
        run_artist = conn.execute('SELECT * FROM run_artist WHERE run_id=? AND artist_id=?', (run_id, artist_id)).fetchone() if run_id else None
        return {'artist': dict(artist), 'run': dict(run_artist) if run_artist else None, 'selected': selected, 'runners_up': others,
                'style_flags': len(flags)}
    finally:
        conn.close()


def _family_posts(conn, sites: tuple[str, ...]):
    marks = ','.join('?' * len(sites))
    return conn.execute(f"""SELECT p.* FROM post p JOIN artist a ON a.id=p.artist_id
                            WHERE a.enabled=1 AND a.site IN ({marks}) ORDER BY p.artist_id""", sites)


def _plan_family(conn, family: str, config: PlannerConfig, log):
    sites = tuple(site for site in SITES if FAMILY[site] == family)
    blocked = config.tag_set('blocked', family)
    boost = config.tag_set('boost', family)
    overrides = {(r['artist_id'], r['site'], r['remote_id']): r['action'] for r in conn.execute('SELECT * FROM override')}
    target_rows = conn.execute('SELECT tag, priority FROM character_target WHERE family=? ORDER BY priority DESC, rank', (family,)).fetchall()
    ranked = [r['tag'] for r in target_rows]
    priority = {r['tag'] for r in target_rows if r['priority']}
    rarity = RarityIndex(config.rarity_min_df)
    rejections = Counter()
    # Pass 1: tag document frequencies over usable posts.
    for row in _family_posts(conn, sites):
        action = overrides.get((row['artist_id'], row['site'], row['remote_id']))
        reason = rejection(row, config, blocked, banned=action == 'ban')
        if reason is None:
            rarity.add(row['general'].split() if row['general'] else ())
    log(f'{family}: indexed {rarity.documents} usable posts')
    # Pass 2: score each artist's usable posts and keep a bounded pool.
    pools, usable_counts = {}, {}
    current, rows = None, []

    def flush():
        if current is None:
            return
        candidates, families = [], set()
        window = rows
        if config.newest_posts_per_artist:
            # Styles drift over the years; recent work is the most consistent.
            window = sorted(rows, key=lambda r: (r['created_at'] or '', int(r['remote_id']) if str(r['remote_id']).isdigit() else 0),
                            reverse=True)[:config.newest_posts_per_artist]
        for r in window:
            action = overrides.get((r['artist_id'], r['site'], r['remote_id']))
            reason = rejection(r, config, blocked, banned=action == 'ban')
            if reason and not (action == 'lock' and reason not in {'no_file', 'banned_by_user'}):
                rejections[reason] += 1
                continue
            c = candidate_from_row(r, locked=action == 'lock')
            families.add(c.family)
            candidates.append(c)
        usable_counts[current] = len(families)
        pools[current] = score_artist(candidates, rarity, boost, set(ranked), config)

    for row in _family_posts(conn, sites):
        if row['artist_id'] != current:
            flush()
            current, rows = row['artist_id'], []
        rows.append(row)
    flush()
    for (artist_id,) in conn.execute(f"SELECT id FROM artist WHERE enabled=1 AND site IN ({','.join('?' * len(sites))})", sites):
        pools.setdefault(artist_id, [])
        usable_counts.setdefault(artist_id, 0)
    log(f'{family}: scoring done for {len(pools)} artists; selecting')
    result = plan_family(pools, usable_counts, config, ranked, priority)
    return result, rejections, ranked


def run_plan(config: PlannerConfig, run_id: int | None = None) -> int:
    """Plan every enabled artist and store the selection as a new run."""
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError('A plan is already running')
    conn = connect()
    try:
        if run_id is None:
            run_id = conn.execute("INSERT INTO run (status, config, created_at) VALUES ('running', ?, ?)",
                                  (config.model_dump_json(), now())).lastrowid
            conn.commit()
        started = time.monotonic()
        log_lines = []

        def log(message):
            log_lines.append(message)

        try:
            summary = {'families': {}, 'log': log_lines}
            for family in ('danbooru', 'e621'):
                result, rejections, ranked = _plan_family(conn, family, config, log)
                kept = {a: v for a, v in result.artists.items() if v['status'] == 'kept'}
                conn.executemany('INSERT INTO run_artist (run_id, artist_id, status, reason, usable, selected, repeats) VALUES (?,?,?,?,?,?,?)',
                                 [(run_id, a, v['status'], v['reason'], v['usable'], v['selected'], v['repeats']) for a, v in result.artists.items()])
                conn.executemany('INSERT INTO selection (run_id, artist_id, site, remote_id, role, pick_order, repeats, gain, reasons) VALUES (?,?,?,?,?,?,?,?,?)',
                                 [(run_id, p['artist_id'], p['site'], p['remote_id'], p['role'], p['pick_order'], p['repeats'], p['gain'], json.dumps(p['reasons']))
                                  for p in result.picks])
                floor = config.character_floor
                covered = [ch for ch in ranked if result.character_counts.get(ch, 0) >= floor] if floor else []
                summary['families'][family] = {
                    'artists_kept': len(kept),
                    'artists_dropped': Counter(v['reason'] for v in result.artists.values() if v['status'] == 'dropped'),
                    'images': len(result.picks),
                    'roles': Counter(p['role'] for p in result.picks),
                    'samples_per_pass': sum(p['repeats'] for p in result.picks),
                    'rejected_posts': rejections,
                    'character_targets': len(ranked),
                    'characters_at_floor': len(covered),
                    'characters_partial': sum(1 for _, n in result.unmet_characters if n > 0),
                    'characters_missing': sum(1 for _, n in result.unmet_characters if n == 0),
                    'unmet_characters': result.unmet_characters[:500],
                }
                conn.commit()
            totals = summary['families'].values()
            summary.update(images=sum(f['images'] for f in totals), samples_per_pass=sum(f['samples_per_pass'] for f in totals),
                           artists_kept=sum(f['artists_kept'] for f in totals), seconds=round(time.monotonic() - started, 1))
            conn.execute("UPDATE run SET status='completed', summary=?, finished_at=? WHERE id=?", (json.dumps(summary), now(), run_id))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            conn.execute("UPDATE run SET status='failed', error=?, finished_at=? WHERE id=?", (str(exc), now(), run_id))
            conn.commit()
            raise
        return run_id
    finally:
        conn.close()
        _run_lock.release()


def start_plan(config: PlannerConfig) -> int:
    """Create the run row immediately and plan in a background thread."""
    if _run_lock.locked():
        raise RuntimeError('A plan is already running')
    conn = connect()
    try:
        run_id = conn.execute("INSERT INTO run (status, config, created_at) VALUES ('running', ?, ?)",
                              (config.model_dump_json(), now())).lastrowid
        conn.commit()
    finally:
        conn.close()

    def work():
        try:
            run_plan(config, run_id)
        except Exception as exc:
            # run_plan records its own failures; this covers a lost lock race.
            conn = connect()
            try:
                conn.execute("UPDATE run SET status='failed', error=?, finished_at=? WHERE id=? AND status='running'", (str(exc), now(), run_id))
                conn.commit()
            finally:
                conn.close()

    threading.Thread(target=work, name=f'planner-run-{run_id}', daemon=True).start()
    return run_id


def get_run(run_id: int) -> dict:
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM run WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise LookupError('Run not found')
        return run_row(row)
    finally:
        conn.close()


def export_manifest(run_id: int) -> Path:
    """Write manifest.jsonl (one line per selected image) and per-site ID lists."""
    run = get_run(run_id)
    if run['status'] != 'completed':
        raise ValueError('Only completed runs can be exported')
    target = planner_dir() / 'exports' / f'run-{run_id}'
    target.mkdir(parents=True, exist_ok=True)
    conn = connect()
    ids = defaultdict(list)
    try:
        temp = target / 'manifest.jsonl.tmp'
        with open(temp, 'w', encoding='utf-8', newline='\n') as handle:
            for r in conn.execute("""SELECT s.*, a.tag artist_tag, a.display_name, p.md5, p.file_url, p.width, p.height,
                                            p.rating, p.characters, p.copyrights, p.artists, p.ext
                                     FROM selection s JOIN artist a ON a.id=s.artist_id
                                     JOIN post p ON p.artist_id=s.artist_id AND p.site=s.site AND p.remote_id=s.remote_id
                                     WHERE s.run_id=? ORDER BY a.site, a.display_name, s.pick_order""", (run_id,)):
                ids[r['site']].append(r['remote_id'])
                handle.write(json.dumps({
                    'site': r['site'], 'remote_id': r['remote_id'], 'artist_tag': r['artist_tag'], 'artist': r['display_name'],
                    'credited_artists': real_artists(r), 'role': r['role'], 'repeats': r['repeats'],
                    'md5': r['md5'], 'file_url': r['file_url'], 'ext': r['ext'], 'motion': (r['ext'] or '') in MOTION_EXTS,
                    'width': r['width'], 'height': r['height'], 'rating': r['rating'],
                    'characters': (r['characters'] or '').split(), 'copyrights': (r['copyrights'] or '').split(),
                    'reasons': json.loads(r['reasons']),
                }, ensure_ascii=False) + '\n')
        temp.replace(target / 'manifest.jsonl')
        for site, values in ids.items():
            (target / f'selected_{site}_ids.txt').write_text('\n'.join(values) + '\n', encoding='utf-8')
        (target / 'summary.json').write_text(json.dumps({'run_id': run_id, 'config': run['config'], 'summary': run['summary']},
                                                        indent=2, ensure_ascii=False), encoding='utf-8')
        return target
    finally:
        conn.close()
