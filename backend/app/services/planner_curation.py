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
    finally:
        main.close()
    return {'artist_id': artist['id'], 'site': artist['site'], 'tag': artist['tag'], 'display_name': artist['display_name'],
            'completed_at': artist['completed_at'], 'images': images, 'locked': locked,
            'target': (json.loads(target['config']).get('max_images') if target else None),
            'planned': target['selected'] if target else None}


def candidates(folder_id: int, include_filtered: bool = False, include_banned: bool = False,
               sort: str = 'popular', offset: int = 0, limit: int = 100) -> dict:
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
        if action == 'ban' and not include_banned:
            continue
        reason = rejection(r, config, blocked)
        if reason and reason != 'banned_by_user' and not include_filtered:
            continue
        items.append({'site': r['site'], 'remote_id': r['remote_id'], 'width': r['width'], 'height': r['height'],
                      'rating': r['rating'], 'fav_count': r['fav_count'], 'score': r['score'], 'created_at': r['created_at'],
                      'ext': r['ext'], 'characters': (r['characters'] or '').split(), 'copyrights': (r['copyrights'] or '').split(),
                      'tags': (r['general'] or '').split(), 'override': action, 'filtered': reason,
                      'url': POST_URLS[r['site']].format(r['remote_id'])})
    if sort == 'newest':
        items.sort(key=lambda i: (i['created_at'] or '', i['remote_id']), reverse=True)
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
    return folder_context(folder_id)
