"""Hand curation of planner collections.

Each planner collection belongs to one planner artist (through its delivery
items). From the collection page you can browse the artist's harvested posts
that are not in the collection, accept some of them (they are locked in the
planner and imported), and mark the whole collection complete: its images
become "accepted", they are locked, and future plans select exactly them.
"""
from __future__ import annotations

import json
from collections import defaultdict

import app.db as db
from app.services.planner_select import PlannerConfig, rejection
from app.services.planner_store import connect, now
from app.services.post_dates import post_year, sortable_time
from app.services.tags import AESTHETIC_MARKS, QUALITY_MARKS, TagService

POST_URLS = {'danbooru': 'https://danbooru.donmai.us/posts/{}', 'e621': 'https://e621.net/posts/{}',
             'gelbooru': 'https://gelbooru.com/index.php?page=post&s=view&id={}'}


def folder_artist(conn, folder_id: int):
    row = conn.execute('SELECT DISTINCT d.artist_id FROM delivery_item d WHERE d.folder_id=? LIMIT 1', (folder_id,)).fetchone()
    return conn.execute('SELECT * FROM artist WHERE id=?', (row['artist_id'],)).fetchone() if row else None


def _folder_posts(folder_id: int) -> tuple[set, dict]:
    """Posts present in a collection, and image IDs per post."""
    present, images = set(), defaultdict(list)
    main = db.get_connection()
    try:
        for r in main.execute('SELECT i.id, s.provider, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?', (folder_id,)):
            key = (r['provider'], str(r['remote_id']).split(':', 1)[0])
            present.add(key)
            if r['id'] not in images[key]:
                images[key].append(r['id'])
        return present, images
    finally:
        main.close()


def _latest_config(conn) -> PlannerConfig:
    row = conn.execute("SELECT config FROM run WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone()
    return PlannerConfig.model_validate(json.loads(row['config'])) if row else PlannerConfig()


def folder_context(folder_id: int) -> dict | None:
    conn = connect()
    try:
        artist = folder_artist(conn, folder_id)
        if not artist:
            return None
        target = conn.execute("""SELECT ra.selected, r.config FROM run_artist ra JOIN run r ON r.id=ra.run_id
                                 WHERE ra.artist_id=? AND ra.status='kept' ORDER BY r.id DESC LIMIT 1""", (artist['id'],)).fetchone()
        locked = conn.execute("SELECT count(*) FROM override WHERE artist_id=? AND action='lock'", (artist['id'],)).fetchone()[0]
    finally:
        conn.close()
    main = db.get_connection()
    try:
        images = main.execute('SELECT count(*) FROM image WHERE folder_id=?', (folder_id,)).fetchone()[0]
        marks = {'total': images, 'viewed': main.execute('SELECT count(*) FROM image WHERE folder_id=? AND marks_viewed_at IS NOT NULL',
                                                          (folder_id,)).fetchone()[0]}
        for source in ('auto', 'manual'):
            marks[source] = main.execute('SELECT count(*) FROM image WHERE folder_id=? AND quality_source=? AND quality_mark IS NOT NULL',
                                         (folder_id, source)).fetchone()[0]
        for column in ('quality_mark', 'aesthetic_mark'):
            for tag, count in main.execute(f'SELECT {column}, count(*) FROM image WHERE folder_id=? AND {column} IS NOT NULL GROUP BY {column}', (folder_id,)):
                marks[tag] = count
        era_from = main.execute('SELECT era_from FROM collection WHERE id=?', (folder_id,)).fetchone()[0]
        dated = [(r['id'], int(r['posted_at'][:4])) for r in main.execute('SELECT id, posted_at FROM image WHERE folder_id=?', (folder_id,))
                 if r['posted_at'] and r['posted_at'][:4].isdigit()]
    finally:
        main.close()
    years = defaultdict(int)
    for _, year in dated:
        years[year] += 1
    return {'era_from': era_from, 'years': {str(year): years[year] for year in sorted(years)},
            'older_ids': [image_id for image_id, year in dated if era_from and year < era_from],
            'undated': images - len(dated), 'marks': marks, 'artist_id': artist['id'], 'site': artist['site'], 'tag': artist['tag'], 'display_name': artist['display_name'],
            'completed_at': artist['completed_at'], 'images': images, 'locked': locked,
            'target': (json.loads(target['config']).get('max_images') if target else None),
            'planned': target['selected'] if target else None}


def candidates(folder_id: int, include_filtered: bool = False, include_banned: bool = False,
               sort: str = 'popular', offset: int = 0, limit: int = 100, min_year: int | None = None) -> dict:
    """The artist's harvested posts that are not in the collection."""
    conn = connect()
    try:
        artist = folder_artist(conn, folder_id)
        if not artist:
            raise LookupError('This collection was not created by the Dataset Planner')
        config = _latest_config(conn)
        family = 'e621' if artist['site'] == 'e621' else 'danbooru'
        blocked = config.tag_set('blocked', family)
        overrides = {(r['site'], r['remote_id']): r['action'] for r in conn.execute('SELECT * FROM override WHERE artist_id=?', (artist['id'],))}
        delivered = {(r['site'], r['remote_id']) for r in conn.execute(
            "SELECT site, remote_id FROM delivery_item WHERE folder_id=? AND status IN ('done', 'skipped')", (folder_id,))}
        rows = conn.execute('SELECT * FROM post WHERE artist_id=?', (artist['id'],)).fetchall()
    finally:
        conn.close()
    present, _ = _folder_posts(folder_id)
    items = []
    for r in rows:
        key = (r['site'], r['remote_id'])
        if key in present:
            continue
        action = overrides.get(key)
        if action is None and key in delivered:
            # Downloaded here and removed by hand: a ban from the next plan on, hidden like one now.
            action = 'ban'
        if action == 'ban' and not include_banned:
            continue
        year = post_year(r['created_at'])
        if min_year and year and year < min_year:
            continue
        reason = rejection(r, config, blocked)
        if reason and reason != 'banned_by_user' and not include_filtered:
            continue
        items.append({'site': r['site'], 'remote_id': r['remote_id'], 'width': r['width'], 'height': r['height'],
                      'rating': r['rating'], 'fav_count': r['fav_count'], 'score': r['score'], 'created_at': r['created_at'],
                      'ext': r['ext'], 'characters': (r['characters'] or '').split(), 'copyrights': (r['copyrights'] or '').split(),
                      'tags': (r['general'] or '').split(), 'override': action, 'filtered': reason, 'year': year or None,
                      'posted_at': sortable_time(r['created_at']) or None,
                      'url': POST_URLS[r['site']].format(r['remote_id'])})
    if sort == 'gaps':
        # Wildcards that add characters still short of their goal across the dataset come first.
        from app.services.tracker import character_needs
        needs = character_needs(family)
        for item in items:
            fills = [{'tag': tag, **{k: needs[tag][k] for k in ('now', 'goal', 'priority')}} for tag in item['characters'] if tag in needs]
            item['fills'] = sorted(fills, key=lambda f: (not f['priority'], f['now'] / f['goal']))
            item['gain'] = round(sum(needs[f['tag']]['need'] for f in fills), 4)
        items.sort(key=lambda i: (-i['gain'], -(i['fav_count'] if i['fav_count'] is not None else (i['score'] or 0)), i['remote_id']))
    elif sort == 'newest':
        items.sort(key=lambda i: (i['posted_at'] or '', int(i['remote_id']) if str(i['remote_id']).isdigit() else 0), reverse=True)
    else:
        items.sort(key=lambda i: (-(i['fav_count'] if i['fav_count'] is not None else (i['score'] or 0)), i['remote_id']))
    page = items[offset:offset + limit]
    return {'artist_id': artist['id'], 'items': page, 'total': len(items),
            'next_offset': offset + limit if offset + limit < len(items) else None}


def accept_posts(folder_id: int, posts: list[tuple[str, str]]) -> dict:
    """Lock the posts for future plans and remember they were accepted into this collection."""
    conn = connect()
    try:
        artist = folder_artist(conn, folder_id)
        if not artist:
            raise LookupError('This collection was not created by the Dataset Planner')
        if artist['completed_at']:
            raise ValueError('This collection is marked complete. Reopen it to change its images.')
        known = {(r['site'], r['remote_id']) for r in conn.execute('SELECT site, remote_id FROM post WHERE artist_id=?', (artist['id'],))}
        accepted = [key for key in dict.fromkeys(posts) if key in known]
        stamp = now()
        conn.executemany("INSERT OR REPLACE INTO override (artist_id, site, remote_id, action, created_at) VALUES (?,?,?,'lock',?)",
                         [(artist['id'], *key, stamp) for key in accepted])
        conn.executemany('INSERT OR REPLACE INTO accepted_post (folder_id, artist_id, site, remote_id, created_at) VALUES (?,?,?,?,?)',
                         [(folder_id, artist['id'], *key, stamp) for key in accepted])
        conn.commit()
        by_site = defaultdict(list)
        for site, remote_id in accepted:
            by_site[site].append(remote_id)
        return {'accepted': len(accepted), 'by_site': dict(by_site)}
    finally:
        conn.close()


def set_era(folder_id: int, era_from: int | None) -> dict:
    """Keep images posted from this year on; older ones are dimmed and wildcards default to the era."""
    if era_from is not None and not 1990 <= int(era_from) <= 2100:
        raise ValueError('Choose a year between 1990 and 2100')
    main = db.get_connection()
    try:
        if not main.execute('UPDATE collection SET era_from=? WHERE id=?', (era_from, folder_id)).rowcount:
            raise LookupError('Folder not found')
        main.commit()
    finally:
        main.close()
    context = folder_context(folder_id)
    if context is None:
        raise LookupError('This collection was not created by the Dataset Planner')
    return context


def _folder_image(main, folder_id: int, image_id: int):
    row = main.execute('''SELECT id, quality_mark, aesthetic_mark, marks_viewed_at, quality_source, quality_auto, quality_auto_info
                          FROM image WHERE id=? AND folder_id=?''', (image_id, folder_id)).fetchone()
    if not row:
        raise LookupError('Image not found in this collection')
    return row


def _require_open(folder_id: int) -> None:
    conn = connect()
    try:
        artist = folder_artist(conn, folder_id)
    finally:
        conn.close()
    if not artist:
        raise LookupError('This collection was not created by the Dataset Planner')
    if artist['completed_at']:
        raise ValueError('This collection is marked complete. Reopen it to change quality marks.')


def set_marks(folder_id: int, image_id: int, quality: str | None, aesthetic: str | None,
              touched: list[str] | None = None, use_auto: bool = False) -> dict:
    """Save an image's working marks; they reach the ground truth when the folder is accepted.

    `touched` lists the scales the user set by hand; a hand-set quality mark
    (including Normal) is never replaced by automatic marks. `use_auto` puts
    the automatic quality mark back.
    """
    if quality not in (None, *QUALITY_MARKS) or aesthetic not in (None, *AESTHETIC_MARKS):
        raise ValueError('Unknown quality or aesthetic mark')
    _require_open(folder_id)
    main = db.get_connection()
    try:
        row = _folder_image(main, folder_id, image_id)
        source = row['quality_source']
        if use_auto:
            quality, source = row['quality_auto'], 'auto'
        elif touched is not None:
            source = 'manual' if 'quality' in touched else source
        elif quality != row['quality_mark']:
            source = 'manual'
        stamp = now()
        main.execute("""UPDATE image SET quality_mark=?, aesthetic_mark=?, quality_source=?,
                        marks_viewed_at=COALESCE(marks_viewed_at, ?) WHERE id=?""",
                     (quality, aesthetic, source, stamp, image_id))
        main.commit()
        return dict(_folder_image(main, folder_id, image_id))
    finally:
        main.close()


def mark_viewed(folder_id: int, image_id: int) -> dict:
    """Opening an image in the viewer counts as reviewing it (it stays normal unless marked)."""
    main = db.get_connection()
    try:
        _folder_image(main, folder_id, image_id)
        main.execute('UPDATE image SET marks_viewed_at=COALESCE(marks_viewed_at, ?) WHERE id=?', (now(), image_id))
        main.commit()
        return dict(_folder_image(main, folder_id, image_id))
    finally:
        main.close()


def commit_marks(folder_id: int) -> int:
    """Write every image's marks into its ground truth (sidecar). Returns images changed."""
    main = db.get_connection()
    try:
        changed = []
        for r in main.execute('SELECT id, quality_mark, aesthetic_mark, quality_tags FROM image WHERE folder_id=?', (folder_id,)).fetchall():
            tags = [tag for tag in (r['quality_mark'], r['aesthetic_mark']) if tag]
            value = json.dumps(tags) if tags else None
            if value != r['quality_tags']:
                changed.append((value, r['id']))
        main.executemany('UPDATE image SET quality_tags=? WHERE id=?', changed)
        main.commit()
    finally:
        main.close()
    TagService(db.LIBRARY_PATH)._rewrite_sidecars([image_id for _, image_id in changed])
    return len(changed)


def set_complete(folder_id: int, complete: bool) -> dict:
    """Complete: accept every image, lock exactly their posts and freeze the artist's selection."""
    conn = connect()
    try:
        artist = folder_artist(conn, folder_id)
        if not artist:
            raise LookupError('This collection was not created by the Dataset Planner')
        present, _ = _folder_posts(folder_id)
        posts = {(r['site'], r['remote_id']) for r in conn.execute('SELECT site, remote_id FROM post WHERE artist_id=?', (artist['id'],))}
        stamp = now()
        if complete:
            mine = [key for key in present if key in posts]
            conn.executemany("INSERT OR REPLACE INTO override (artist_id, site, remote_id, action, created_at) VALUES (?,?,?,'lock',?)",
                             [(artist['id'], *key, stamp) for key in mine])
            # The collection is the selection now: locks on posts that are not in it go.
            locked = conn.execute("SELECT site, remote_id FROM override WHERE artist_id=? AND action='lock'", (artist['id'],)).fetchall()
            conn.executemany('DELETE FROM override WHERE artist_id=? AND site=? AND remote_id=?',
                             [(artist['id'], r['site'], r['remote_id']) for r in locked if (r['site'], r['remote_id']) not in present])
            conn.execute('DELETE FROM accepted_post WHERE artist_id=?', (artist['id'],))
            conn.execute('UPDATE artist SET completed_at=? WHERE id=?', (stamp, artist['id']))
        else:
            # Its images stay locked; record them as accepted so removing one later bans it.
            locked = {(r['site'], r['remote_id']) for r in conn.execute("SELECT site, remote_id FROM override WHERE artist_id=? AND action='lock'", (artist['id'],))}
            conn.executemany('INSERT OR REPLACE INTO accepted_post (folder_id, artist_id, site, remote_id, created_at, seen) VALUES (?,?,?,?,?,1)',
                             [(folder_id, artist['id'], *key, stamp) for key in present if key in locked])
            conn.execute('UPDATE artist SET completed_at=NULL WHERE id=?', (artist['id'],))
        conn.commit()
    finally:
        conn.close()
    main = db.get_connection()
    try:
        main.execute('UPDATE image SET review_status=? WHERE folder_id=? AND review_status=?',
                     ('accepted', folder_id, 'pending') if complete else ('pending', folder_id, 'accepted'))
        main.commit()
    finally:
        main.close()
    if complete:
        # Reopening keeps the committed tags until the folder is accepted again.
        commit_marks(folder_id)
    return folder_context(folder_id)
