"""Dataset Tracker: character and general-tag coverage across all planner collections.

The planner collections form one growing dataset: the pilot, then each batch of
artists delivered into the same groups. For every target character and every
general tag the tracker reports how many images the plans picked, how many are
in the collections now, how many are in accepted collections, the goal (the
planner's character floor, or a goal you set) and how many unused harvested
posts the wildcards could still add.

Characters are stored per collection (`tracker_folder_char`) and recomputed for
one collection whenever it is opened or edited, so the global character totals
stay current while you curate. General tags have tens of thousands of distinct
values and are aggregated by the background refresh only.
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from typing import Callable, Optional

import app.db as db
from app.services.planner_select import PlannerConfig, rejection, split
from app.services.planner_store import connect, now
from app.services.post_dates import post_year

KINDS = ('character', 'general')
STATUSES = ('missing', 'lost', 'below', 'met')
PRIORITY_BOOST = 1.5


def family_of(site: str) -> str:
    return 'e621' if site == 'e621' else 'danbooru'


def _latest_config(conn) -> PlannerConfig:
    row = conn.execute("SELECT config FROM run WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone()
    return PlannerConfig.model_validate(json.loads(row['config'])) if row else PlannerConfig()


def _planner_folders(conn) -> dict[int, dict]:
    """Every collection the planner delivered into, with its artist."""
    folders = {}
    for r in conn.execute("""SELECT d.folder_id, d.artist_id, a.site, a.tag, a.display_name, a.completed_at
                             FROM (SELECT DISTINCT folder_id, artist_id FROM delivery_item) d JOIN artist a ON a.id=d.artist_id"""):
        folders.setdefault(r['folder_id'], dict(r))
    return folders


def _existing_folders(main) -> dict[int, dict]:
    return {r['id']: dict(r) for r in main.execute(
        """SELECT c.id, c.name, c.era_from, g.name AS group_name, g.id AS group_id,
                  (SELECT count(*) FROM image i WHERE i.folder_id=c.id) AS images
           FROM collection c LEFT JOIN artist_group g ON g.id=c.group_id""")}


class FolderScan:
    """Tags of one collection: images now, posts the plan picked, unused usable posts."""

    def __init__(self):
        self.images = 0
        self.planned = 0
        self.now = {kind: Counter() for kind in KINDS}
        self.planned_tags = {kind: Counter() for kind in KINDS}
        self.spare = {kind: Counter() for kind in KINDS}
        self.series = Counter()  # (character, copyright) co-occurrence


def scan_folder(planner, main, folder: dict, config: PlannerConfig, era_from: Optional[int], general: bool) -> FolderScan:
    folder_id, artist_id, site = folder['folder_id'], folder['artist_id'], folder['site']
    completed = bool(folder.get('completed_at'))
    scan = FolderScan()
    kinds = KINDS if general else ('character',)

    image_post: dict[int, tuple] = {}
    for r in main.execute("""SELECT i.id, s.provider, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id
                             WHERE i.folder_id=? ORDER BY s.id""", (folder_id,)):
        key = (r['provider'], str(r['remote_id']).split(':', 1)[0])
        if r['id'] not in image_post or (key[0] == site and image_post[r['id']][0] != site):
            image_post[r['id']] = key
    untagged = [image_id for image_id, in main.execute('SELECT id FROM image WHERE folder_id=?', (folder_id,)) if image_id not in image_post]
    scan.images = len(image_post) + len(untagged)

    latest = planner.execute('SELECT max(delivery_id) FROM delivery_item WHERE folder_id=?', (folder_id,)).fetchone()[0]
    planned = {(r['site'], r['remote_id']) for r in planner.execute(
        'SELECT site, remote_id FROM delivery_item WHERE delivery_id=? AND folder_id=?', (latest, folder_id))} if latest else set()
    scan.planned = len(planned)

    present = set(image_post.values())
    if completed:
        wanted = present | planned
        posts = {}
        by_site = defaultdict(list)
        for key in wanted:
            by_site[key[0]].append(key[1])
        for post_site, ids in by_site.items():
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                for r in planner.execute(f"SELECT * FROM post WHERE artist_id=? AND site=? AND remote_id IN ({','.join('?' * len(chunk))})",
                                         (artist_id, post_site, *chunk)):
                    posts[(r['site'], r['remote_id'])] = r
    else:
        posts = {(r['site'], r['remote_id']): r for r in planner.execute('SELECT * FROM post WHERE artist_id=?', (artist_id,))}

    def tags_of(row) -> dict[str, tuple]:
        return {'character': split(row['characters']), 'general': split(row['general']) if general else ()}

    for key in image_post.values():
        row = posts.get(key)
        if row is None:
            continue
        for kind, tags in tags_of(row).items():
            scan.now[kind].update(tags)
        for character in split(row['characters']):
            for copyright in split(row['copyrights']):
                scan.series[(character, copyright)] += 1
    if untagged:
        for start in range(0, len(untagged), 500):
            chunk = untagged[start:start + 500]
            seen = set()
            for r in main.execute(f"""SELECT image_id, category, tag FROM image_tag
                                      WHERE category IN ('character', 'general') AND image_id IN ({','.join('?' * len(chunk))})""", chunk):
                item = (r['image_id'], r['category'], r['tag'].replace(' ', '_'))
                if item not in seen and r['category'] in kinds:
                    seen.add(item)
                    scan.now[r['category']][item[2]] += 1
    for key in planned:
        row = posts.get(key)
        if row is not None:
            for kind, tags in tags_of(row).items():
                scan.planned_tags[kind].update(tags)
    if not completed:
        family = family_of(site)
        blocked = config.tag_set('blocked', family)
        banned = {(r['site'], r['remote_id']) for r in planner.execute("SELECT site, remote_id FROM override WHERE artist_id=? AND action='ban'", (artist_id,))}
        # Posts downloaded here and removed by hand become bans at the next plan; they are not spare.
        banned |= {(r['site'], r['remote_id']) for r in planner.execute(
            "SELECT site, remote_id FROM delivery_item WHERE folder_id=? AND status IN ('done', 'skipped')", (folder_id,))}
        for key, row in posts.items():
            if key in present or key in banned or rejection(row, config, blocked):
                continue
            if era_from:
                year = post_year(row['created_at'])
                if year and year < era_from:
                    continue
            for kind, tags in tags_of(row).items():
                scan.spare[kind].update(tags)
            for character in split(row['characters']):
                for copyright in split(row['copyrights']):
                    scan.series[(character, copyright)] += 1
    return scan


def _store_folder(planner, folder: dict, scan: FolderScan) -> None:
    family = family_of(folder['site'])
    folder_id = folder['folder_id']
    planner.execute('DELETE FROM tracker_folder_char WHERE folder_id=?', (folder_id,))
    tags = set(scan.now['character']) | set(scan.planned_tags['character']) | set(scan.spare['character'])
    planner.executemany('INSERT INTO tracker_folder_char (folder_id, family, tag, now, planned, spare) VALUES (?, ?, ?, ?, ?, ?)',
                        [(folder_id, family, tag, scan.now['character'][tag], scan.planned_tags['character'][tag], scan.spare['character'][tag])
                         for tag in tags])
    planner.execute("""INSERT INTO tracker_folder (folder_id, artist_id, family, images, planned, refreshed_at) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(folder_id) DO UPDATE SET artist_id=excluded.artist_id, family=excluded.family, images=excluded.images,
                       planned=excluded.planned, refreshed_at=excluded.refreshed_at""",
                    (folder_id, folder['artist_id'], family, scan.images, scan.planned, now()))


def refresh_folder(folder_id: int) -> Optional[FolderScan]:
    """Recompute one collection's character counts (one artist's posts; fast)."""
    planner = connect()
    main = db.get_connection()
    try:
        folders = _planner_folders(planner)
        folder = folders.get(folder_id)
        era = main.execute('SELECT era_from FROM collection WHERE id=?', (folder_id,)).fetchone()
        if not folder or era is None:
            return None
        scan = scan_folder(planner, main, folder, _latest_config(planner), era[0], general=False)
        _store_folder(planner, folder, scan)
        planner.commit()
        return scan
    finally:
        main.close()
        planner.close()


def refresh_all(progress: Callable[..., None] = lambda **_: None) -> dict:
    """Recompute every collection's characters, plus general tags and character series."""
    planner = connect()
    main = db.get_connection()
    try:
        config = _latest_config(planner)
        folders = _planner_folders(planner)
        existing = _existing_folders(main)
        folders = {fid: f for fid, f in folders.items() if fid in existing}
        planner.execute('DELETE FROM tracker_folder WHERE folder_id NOT IN (SELECT value FROM json_each(?))', (json.dumps(list(folders)),))
        planner.execute('DELETE FROM tracker_folder_char WHERE folder_id NOT IN (SELECT folder_id FROM tracker_folder)')
        general = defaultdict(lambda: [0, 0, 0, 0, 0, 0])  # now, accepted, planned, folders, spare, spare artists
        series = Counter()
        progress(done=0, total=len(folders))
        for index, (folder_id, folder) in enumerate(sorted(folders.items()), 1):
            scan = scan_folder(planner, main, folder, config, existing[folder_id]['era_from'], general=True)
            _store_folder(planner, folder, scan)
            family = family_of(folder['site'])
            for tag, count in scan.now['general'].items():
                entry = general[(family, tag)]
                entry[0] += count
                entry[1] += count if folder['completed_at'] else 0
                entry[3] += 1
            for tag, count in scan.planned_tags['general'].items():
                general[(family, tag)][2] += count
            for tag, count in scan.spare['general'].items():
                entry = general[(family, tag)]
                entry[4] += count
                entry[5] += 1
            for (character, copyright), count in scan.series.items():
                series[(family, character, copyright)] += count
            if index % 25 == 0:
                planner.commit()
            progress(done=index, total=len(folders))
        planner.execute('DELETE FROM tracker_general')
        planner.executemany('INSERT INTO tracker_general (family, tag, now, accepted, planned, folders, spare, spare_artists) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            [(family, tag, *values) for (family, tag), values in general.items()])
        best = {}
        for (family, character, copyright), count in series.items():
            if count > best.get((family, character), ('', 0))[1]:
                best[(family, character)] = (copyright, count)
        planner.execute('DELETE FROM tracker_series')
        planner.executemany('INSERT INTO tracker_series (family, tag, series) VALUES (?, ?, ?)',
                            [(family, character, copyright) for (family, character), (copyright, _) in best.items()])
        meta = {'refreshed_at': now(), 'folders': len(folders)}
        planner.execute("INSERT INTO planner_setting (key, value) VALUES ('tracker_meta', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (json.dumps(meta),))
        planner.commit()
        return meta
    finally:
        main.close()
        planner.close()


def meta() -> Optional[dict]:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM planner_setting WHERE key='tracker_meta'").fetchone()
        return json.loads(row['value']) if row else None
    finally:
        conn.close()


def _floor(conn) -> int:
    return _latest_config(conn).character_floor


def _goals(conn, kind: str) -> dict[tuple, int]:
    return {(r['family'], r['tag']): r['goal'] for r in conn.execute('SELECT family, tag, goal FROM tracker_goal WHERE kind=?', (kind,))}


def set_goal(kind: str, family: str, tags: list[str], goal: Optional[int]) -> int:
    conn = connect()
    try:
        if goal is None:
            conn.executemany('DELETE FROM tracker_goal WHERE kind=? AND family=? AND tag=?', [(kind, family, tag) for tag in tags])
        else:
            conn.executemany("""INSERT INTO tracker_goal (kind, family, tag, goal) VALUES (?, ?, ?, ?)
                                ON CONFLICT(kind, family, tag) DO UPDATE SET goal=excluded.goal""", [(kind, family, tag, goal) for tag in tags])
        conn.commit()
        return len(tags)
    finally:
        conn.close()


def series_characters(family: str, series: str) -> list[str]:
    conn = connect()
    try:
        return [r['tag'] for r in conn.execute('SELECT tag FROM tracker_series WHERE family=? AND series=?', (family, series))]
    finally:
        conn.close()


def _character_aggregates(conn) -> dict[tuple, dict]:
    rows = conn.execute("""SELECT c.family, c.tag, SUM(c.now) AS now,
                                  SUM(CASE WHEN a.completed_at IS NOT NULL THEN c.now ELSE 0 END) AS accepted,
                                  SUM(c.planned) AS planned,
                                  SUM(CASE WHEN a.completed_at IS NULL THEN c.spare ELSE 0 END) AS spare,
                                  SUM(CASE WHEN a.completed_at IS NULL AND c.spare > 0 THEN 1 ELSE 0 END) AS spare_artists,
                                  SUM(CASE WHEN c.now > 0 THEN 1 ELSE 0 END) AS folders
                           FROM tracker_folder_char c JOIN tracker_folder f ON f.folder_id=c.folder_id JOIN artist a ON a.id=f.artist_id
                           GROUP BY c.family, c.tag""")
    return {(r['family'], r['tag']): dict(r) for r in rows}


def _status(row: dict) -> str:
    if row['target'] and row['now'] == 0:
        return 'missing'
    if row['now'] < row['planned']:
        return 'lost'
    if row['goal'] and row['now'] < row['goal']:
        return 'below'
    return 'met'


def table_rows(kind: str, snapshot_id: Optional[int] = None) -> list[dict]:
    """Every tracked tag of a kind with its counts, goal and status."""
    conn = connect()
    try:
        goals = _goals(conn, kind)
        snapshot = {}
        if snapshot_id:
            snapshot = {(r['family'], r['tag']): dict(r) for r in conn.execute(
                'SELECT family, tag, now, accepted, planned FROM tracker_snapshot_stat WHERE snapshot_id=? AND kind=?', (snapshot_id, kind))}
        rows: dict[tuple, dict] = {}
        empty = {'now': 0, 'accepted': 0, 'planned': 0, 'spare': 0, 'spare_artists': 0, 'folders': 0}
        if kind == 'character':
            floor = _floor(conn)
            series = {(r['family'], r['tag']): r['series'] for r in conn.execute('SELECT family, tag, series FROM tracker_series')}
            targets = {(r['family'], r['tag']): r for r in conn.execute('SELECT family, tag, priority, rank FROM character_target')}
            aggregates = _character_aggregates(conn)
            for key in set(targets) | set(aggregates):
                target = targets.get(key)
                values = aggregates.get(key, empty)
                rows[key] = {'family': key[0], 'tag': key[1], 'series': series.get(key, ''), 'target': target is not None,
                             'priority': bool(target and target['priority']), 'rank': target['rank'] if target else None,
                             **{name: int(values[name] or 0) for name in empty},
                             'floor': floor if target else None, 'custom_goal': goals.get(key)}
        else:
            boost = {family: _latest_config(conn).tag_set('boost', family) for family in ('danbooru', 'e621')}
            for r in conn.execute('SELECT * FROM tracker_general'):
                key = (r['family'], r['tag'])
                rows[key] = {'family': r['family'], 'tag': r['tag'], 'series': '', 'target': False, 'boost': r['tag'] in boost[r['family']],
                             'priority': False, 'rank': None, **{name: r[name] for name in empty}, 'floor': None, 'custom_goal': goals.get(key)}
        for key, row in rows.items():
            row['goal'] = row['custom_goal'] if row['custom_goal'] is not None else row['floor']
            if kind == 'general':
                # Curation removes many tagged images on purpose; general tags only get a status against your goal.
                row['status'] = ('below' if row['now'] < row['goal'] else 'met') if row['goal'] else ''
            else:
                row['status'] = _status(row)
            row['gap'] = max(0, (row['goal'] or 0) - row['now'])
            previous = snapshot.get(key)
            row['delta'] = row['now'] - (previous['now'] if previous else 0) if snapshot_id else None
        return list(rows.values())
    finally:
        conn.close()


SORTS = {
    'gap': lambda r: (-int(r['priority']), -r['gap'], -r['planned'], r['tag']),
    'now': lambda r: (-r['now'], r['tag']),
    'lost': lambda r: (-(r['planned'] - r['now']), r['tag']),
    'spare': lambda r: (-r['spare'], r['tag']),
    'rank': lambda r: (r['rank'] is None, r['rank'] or 0, r['tag']),
    'name': lambda r: r['tag'],
    'delta': lambda r: ((r['delta'] or 0), r['tag']),
}


def table(kind: str, family: Optional[str] = None, search: str = '', status: Optional[str] = None, series: str = '',
          priority_only: bool = False, targets_only: bool = False, boost_only: bool = False, present_only: bool = False,
          sort: str = 'gap', snapshot_id: Optional[int] = None, offset: int = 0, limit: int = 100) -> dict:
    rows = table_rows(kind, snapshot_id)
    needle = search.strip().casefold().replace(' ', '_')
    def keep(row):
        return ((not family or row['family'] == family) and (not needle or needle in row['tag'].casefold())
                and (not status or row['status'] == status) and (not series or row['series'] == series)
                and (not priority_only or row['priority']) and (not targets_only or row['target'])
                and (not boost_only or row.get('boost')) and (not present_only or row['now'] > 0))
    selected = [row for row in rows if keep(row)]
    selected.sort(key=SORTS.get(sort, SORTS['gap']))
    in_family = [row for row in rows if not family or row['family'] == family]
    summary = {
        'tags': len(in_family), 'present': sum(row['now'] > 0 for row in in_family),
        'targets': sum(row['target'] for row in in_family),
        'statuses': dict(Counter(row['status'] for row in in_family if row['status'] and (row['target'] or row['goal'] or kind == 'character'))),
        'priority_below': sum(1 for row in in_family if row['priority'] and row['status'] != 'met'),
        'lost_images': sum(max(0, row['planned'] - row['now']) for row in in_family),
    }
    series_counts = Counter(row['series'] for row in in_family if row['series'])
    return {'items': selected[offset:offset + limit], 'total': len(selected), 'offset': offset,
            'next_offset': offset + limit if offset + limit < len(selected) else None, 'summary': summary,
            'series': [name for name, _ in series_counts.most_common(300)]}


def folders() -> dict:
    """Planner collections with their review state, for the review list and the sidebar."""
    planner = connect()
    main = db.get_connection()
    try:
        existing = _existing_folders(main)
        listed = _planner_folders(planner)
        row = planner.execute("SELECT config FROM run WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone()
        default_target = json.loads(row['config']).get('max_images') if row else None
        items = []
        for folder_id, folder in listed.items():
            info = existing.get(folder_id)
            if not info:
                continue
            items.append({'folder_id': folder_id, 'name': info['name'], 'group': info['group_name'], 'group_id': info['group_id'],
                          'site': folder['site'], 'artist': folder['display_name'], 'images': info['images'],
                          'target': default_target, 'completed_at': folder['completed_at']})
        items.sort(key=lambda item: ((item['group'] or '').casefold(), item['name'].casefold()))
        return {'items': items, 'accepted': sum(bool(item['completed_at']) for item in items), 'total': len(items),
                'images': sum(item['images'] for item in items),
                'accepted_images': sum(item['images'] for item in items if item['completed_at'])}
    finally:
        main.close()
        planner.close()


def _global(conn, family: str, tags: list[str]) -> dict[str, dict]:
    if not tags:
        return {}
    floor = _floor(conn)
    goals = _goals(conn, 'character')
    placeholders = ','.join('?' * len(tags))
    totals = {r['tag']: dict(r) for r in conn.execute(f"""SELECT c.tag, SUM(c.now) AS now, SUM(c.planned) AS planned,
                                                               SUM(CASE WHEN a.completed_at IS NOT NULL THEN c.now ELSE 0 END) AS accepted
                                                        FROM tracker_folder_char c JOIN tracker_folder f ON f.folder_id=c.folder_id
                                                        JOIN artist a ON a.id=f.artist_id
                                                        WHERE c.family=? AND c.tag IN ({placeholders}) GROUP BY c.tag""", (family, *tags))}
    targets = {r['tag']: r for r in conn.execute(f'SELECT tag, priority FROM character_target WHERE family=? AND tag IN ({placeholders})', (family, *tags))}
    series = {r['tag']: r['series'] for r in conn.execute(f'SELECT tag, series FROM tracker_series WHERE family=? AND tag IN ({placeholders})', (family, *tags))}
    result = {}
    for tag in tags:
        total = totals.get(tag, {})
        target = targets.get(tag)
        goal = goals.get((family, tag), floor if target else None)
        row = {'tag': tag, 'now': int(total.get('now') or 0), 'planned': int(total.get('planned') or 0), 'accepted': int(total.get('accepted') or 0),
               'goal': goal, 'target': target is not None, 'priority': bool(target and target['priority']), 'series': series.get(tag, '')}
        row['status'] = _status(row)
        result[tag] = row
    return result


def folder_detail(folder_id: int) -> Optional[dict]:
    """Characters in one collection with live dataset-wide totals; refreshes the collection first."""
    scan = refresh_folder(folder_id)
    if scan is None:
        return None
    planner = connect()
    try:
        folder = _planner_folders(planner)[folder_id]
        family = family_of(folder['site'])
        here = scan.now['character']
        lost = {tag: count for tag, count in scan.planned_tags['character'].items() if here[tag] < count}
        totals = _global(planner, family, sorted(set(here) | set(lost)))
    finally:
        planner.close()
    characters = sorted(({**totals[tag], 'here': count, 'planned_here': scan.planned_tags['character'][tag]} for tag, count in here.items()),
                        key=lambda row: (not row['priority'], row['status'] == 'met', -row['here'], row['tag']))
    return {'folder_id': folder_id, 'family': family, 'images': scan.images, 'characters': characters,
            'lost': sorted(({**totals[tag], 'planned_here': count, 'here': here[tag]} for tag, count in lost.items()),
                           key=lambda row: (not row['priority'], row['tag']))}


def character_folders(family: str, tag: str) -> list[dict]:
    """Collections that contain a character, were planned to, or could still add it from their wildcards."""
    planner = connect()
    main = db.get_connection()
    try:
        existing = _existing_folders(main)
        rows = planner.execute("""SELECT c.folder_id, c.now, c.planned, c.spare, a.completed_at, a.display_name
                                  FROM tracker_folder_char c JOIN tracker_folder f ON f.folder_id=c.folder_id JOIN artist a ON a.id=f.artist_id
                                  WHERE c.family=? AND c.tag=?""", (family, tag)).fetchall()
    finally:
        main.close()
        planner.close()
    result = [{'folder_id': r['folder_id'], 'name': existing[r['folder_id']]['name'], 'group': existing[r['folder_id']]['group_name'],
               'now': r['now'], 'planned': r['planned'], 'spare': 0 if r['completed_at'] else r['spare'], 'accepted': bool(r['completed_at'])}
              for r in rows if r['folder_id'] in existing]
    return sorted(result, key=lambda r: (-r['now'], -r['spare'], r['name'].casefold()))


def deletion_impact(folder_id: int, image_ids: list[int]) -> dict:
    """After images were removed: which of their characters are now short of their goal."""
    main = db.get_connection()
    try:
        placeholders = ','.join('?' * len(image_ids)) or 'NULL'
        keys = {(r['provider'], str(r['remote_id']).split(':', 1)[0]) for r in main.execute(
            f'SELECT provider, remote_id FROM image_source WHERE image_id IN ({placeholders})', image_ids)}
        fallback = {r['tag'].replace(' ', '_') for r in main.execute(
            f"SELECT tag FROM image_tag WHERE category='character' AND image_id IN ({placeholders})", image_ids)}
    finally:
        main.close()
    planner = connect()
    try:
        folder = _planner_folders(planner).get(folder_id)
        if not folder:
            return {'characters': []}
        characters = set(fallback)
        for site, remote_id in keys:
            row = planner.execute('SELECT characters FROM post WHERE artist_id=? AND site=? AND remote_id=?', (folder['artist_id'], site, remote_id)).fetchone()
            if row:
                characters.update(split(row['characters']))
        family = family_of(folder['site'])
    finally:
        planner.close()
    refresh_folder(folder_id)
    planner = connect()
    try:
        totals = _global(planner, family, sorted(characters))
    finally:
        planner.close()
    short = [row for row in totals.values() if row['goal'] and row['now'] < row['goal']]
    short.sort(key=lambda row: (not row['priority'], row['now'], row['tag']))
    return {'characters': short}


def character_needs(family: str) -> dict[str, dict]:
    """Characters below their goal, with how much each still needs (for ranking wildcards)."""
    conn = connect()
    try:
        floor = _floor(conn)
        goals = _goals(conn, 'character')
        aggregates = _character_aggregates(conn)
        needs = {}
        for r in conn.execute('SELECT tag, priority FROM character_target WHERE family=?', (family,)):
            goal = goals.get((family, r['tag']), floor)
            have = int(aggregates.get((family, r['tag']), {}).get('now') or 0)
            if goal and have < goal:
                needs[r['tag']] = {'now': have, 'goal': goal, 'priority': bool(r['priority']),
                                   'need': (goal - have) / goal * (PRIORITY_BOOST if r['priority'] else 1.0)}
        for (goal_family, tag), goal in goals.items():
            if goal_family == family and tag not in needs:
                have = int(aggregates.get((family, tag), {}).get('now') or 0)
                if goal and have < goal:
                    needs[tag] = {'now': have, 'goal': goal, 'priority': False, 'need': (goal - have) / goal}
        return needs
    finally:
        conn.close()


def create_snapshot(name: str) -> dict:
    name = ' '.join(str(name).split())
    if not name:
        raise ValueError('Name the snapshot, for example v0.4')
    characters = table_rows('character')
    general = table_rows('general')
    summary = folders()
    conn = connect()
    try:
        if conn.execute('SELECT 1 FROM tracker_snapshot WHERE name=?', (name,)).fetchone():
            raise ValueError(f'A snapshot named "{name}" already exists')
        snapshot_id = conn.execute('INSERT INTO tracker_snapshot (name, created_at, summary) VALUES (?, ?, ?)', (name, now(), json.dumps({
            'folders': summary['total'], 'accepted': summary['accepted'], 'images': summary['images'], 'accepted_images': summary['accepted_images'],
            'characters_present': sum(r['now'] > 0 for r in characters), 'general_present': sum(r['now'] > 0 for r in general),
            'general_refreshed_at': (meta() or {}).get('refreshed_at')}))).lastrowid
        conn.executemany('INSERT INTO tracker_snapshot_stat (snapshot_id, kind, family, tag, now, accepted, planned) VALUES (?, ?, ?, ?, ?, ?, ?)',
                         [(snapshot_id, kind, r['family'], r['tag'], r['now'], r['accepted'], r['planned'])
                          for kind, rows in (('character', characters), ('general', general)) for r in rows if r['now'] or r['planned']])
        conn.commit()
        return snapshots()[0]
    finally:
        conn.close()


def snapshots() -> list[dict]:
    conn = connect()
    try:
        return [{**dict(r), 'summary': json.loads(r['summary'])} for r in conn.execute('SELECT * FROM tracker_snapshot ORDER BY id DESC')]
    finally:
        conn.close()


def delete_snapshot(snapshot_id: int) -> bool:
    conn = connect()
    try:
        conn.execute('DELETE FROM tracker_snapshot_stat WHERE snapshot_id=?', (snapshot_id,))
        deleted = conn.execute('DELETE FROM tracker_snapshot WHERE id=?', (snapshot_id,)).rowcount
        conn.commit()
        return bool(deleted)
    finally:
        conn.close()


CSV_COLUMNS = ['kind', 'family', 'tag', 'series', 'target', 'priority', 'boost', 'rank', 'planned', 'now', 'accepted', 'folders',
               'floor', 'custom_goal', 'goal', 'gap', 'status', 'spare', 'spare_artists', 'delta']


def export_csv(kind: str, snapshot_id: Optional[int] = None) -> str:
    """The current table (with change since `snapshot_id`), or a snapshot's stored counts."""
    out = io.StringIO()
    writer = csv.DictWriter(out, CSV_COLUMNS, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for row in sorted(table_rows(kind, snapshot_id), key=SORTS['rank'] if kind == 'character' else SORTS['now']):
        writer.writerow({**row, 'kind': kind, 'tag': row['tag'].replace('_', ' ') if kind == 'general' else row['tag']})
    return out.getvalue()


def export_snapshot_csv(snapshot_id: int) -> tuple[str, str]:
    conn = connect()
    try:
        snapshot = conn.execute('SELECT name FROM tracker_snapshot WHERE id=?', (snapshot_id,)).fetchone()
        if not snapshot:
            raise LookupError('Snapshot not found')
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\n')
        writer.writerow(['kind', 'family', 'tag', 'planned', 'now', 'accepted'])
        for r in conn.execute('SELECT kind, family, tag, planned, now, accepted FROM tracker_snapshot_stat WHERE snapshot_id=? ORDER BY kind, now DESC, tag',
                              (snapshot_id,)):
            writer.writerow(list(r))
        return snapshot['name'], out.getvalue()
    finally:
        conn.close()
