"""Dataset Planner delivery: download a run's selected posts into collections.

Each site gets a group (default "Planner <site>") and each artist a normal
artist collection inside it, so delivered images get the usual sidecars,
artist trigger, deduplication, motion-frame extraction and QA. Planner folders
are protected from Sync All, scheduled and group syncs, which would otherwise
scrape artists' whole galleries.

Fresh metadata is looked up 100 posts per request (`id:` search on Danbooru
and e621; Gelbooru one post at a time) and every post's outcome is stored, so
a stopped or interrupted delivery resumes where it left off.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Callable

import app.db as db
from app.models import FolderCreate
from app.providers.booru import ProviderAuthenticationError
from app.providers.registry import create_provider
from app.services.diagnostics import emit
from app.services.media import StaticPreviewUnavailable
from app.services.planner_harvest import TRANSIENT_ATTEMPTS, _transient
from app.services.planner_store import connect, now, planner_dir
from app.services.post_ingest import ingest_post
from app.services.queries import humanize_artist_query

LOOKUP_BATCH = 100
ACTIVE = ('queued', 'running', 'cancelling')
_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None


def _row(row) -> dict:
    data = dict(row)
    data['progress'] = json.loads(data['progress'])
    return data


def get_delivery(delivery_id: int | None = None) -> dict | None:
    conn = connect()
    try:
        row = conn.execute('SELECT * FROM delivery WHERE id=?', (delivery_id,)).fetchone() if delivery_id else \
            conn.execute('SELECT * FROM delivery ORDER BY id DESC LIMIT 1').fetchone()
        if not row:
            return None
        data = _row(row)
        data['counts'] = {f"{r['site']}:{r['status']}": r['n'] for r in conn.execute(
            'SELECT site, status, count(*) n FROM delivery_item WHERE delivery_id=? GROUP BY site, status', (data['id'],))}
        return data
    finally:
        conn.close()


def _ensure_group(main, name: str, site: str, library: Path) -> int:
    from app.services.groups import create_group
    row = main.execute('SELECT id, provider FROM artist_group WHERE name=? COLLATE NOCASE', (name,)).fetchone()
    if row:
        if row['provider'] != site:
            raise ValueError(f'Group "{name}" already exists for {row["provider"]}; choose another group name prefix')
        return row['id']
    return create_group(name, site, library)['id']


def _ensure_folders(group_id: int, site: str, artists: list[dict], library: Path) -> dict[int, int]:
    """Reuse or create one protected artist folder per planner artist; returns artist_id -> folder_id."""
    from app.services.collections import CollectionService
    from app.services.groups import ensure_folder_directories
    main = db.get_connection()
    mapping, created_dirs = {}, []
    try:
        main.execute('BEGIN IMMEDIATE')
        by_tag, by_name = {}, {}
        for row in main.execute("""SELECT c.id, c.name, s.query_override FROM collection c
                                   LEFT JOIN collection_source s ON s.collection_id=c.id AND s.provider=?
                                   WHERE c.group_id=? AND c.type='artist'""", (site, group_id)):
            if row['query_override']:
                by_tag[row['query_override']] = row['id']
            by_name[row['name'].casefold()] = row['id']
        service = CollectionService()
        for artist in artists:
            folder_id = by_tag.get(artist['tag'])
            if folder_id is None:
                name = humanize_artist_query(artist['display_name']) or artist['tag'].replace('_', ' ')
                if name.casefold() in by_name:
                    name = f"{name} ({artist['tag']})"
                folder = service.create_collection(FolderCreate(
                    name=name, query=name, group_id=group_id, type='artist', sources=[site],
                    source_queries={site: artist['tag']}), connection=main)
                folder_id = folder.id
                by_tag[artist['tag']] = folder_id
                by_name[name.casefold()] = folder_id
                slug = main.execute('SELECT slug FROM collection WHERE id=?', (folder_id,)).fetchone()[0]
                created_dirs.extend(ensure_folder_directories(library, slug))
            main.execute('INSERT OR IGNORE INTO group_blocked_folder(group_id, folder_id, created_at) VALUES (?,?,?)',
                         (group_id, folder_id, now()))
            mapping[artist['artist_id']] = folder_id
        main.commit()
        return mapping
    except Exception:
        main.rollback()
        from app.services.groups import remove_empty_directories
        remove_empty_directories(created_dirs)
        raise
    finally:
        main.close()


def create_delivery(run_id: int, group_prefix: str, library: Path) -> dict:
    group_prefix = ' '.join(group_prefix.split()) or 'Planner'
    if _task and not _task.done():
        raise RuntimeError('A delivery is already running')
    conn = connect()
    try:
        run = conn.execute('SELECT status FROM run WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise LookupError('Run not found')
        if run['status'] != 'completed':
            raise ValueError('Only completed runs can be delivered')
        if conn.execute(f"SELECT 1 FROM delivery WHERE status IN {ACTIVE}").fetchone():
            raise RuntimeError('A delivery is already running')
        selections = conn.execute("""SELECT s.artist_id, s.site, s.remote_id, a.tag, a.display_name FROM selection s
                                     JOIN artist a ON a.id=s.artist_id WHERE s.run_id=? ORDER BY s.site, s.artist_id, s.pick_order""",
                                  (run_id,)).fetchall()
    finally:
        conn.close()
    if not selections:
        raise ValueError('This run selected no images')
    by_site = defaultdict(dict)
    for row in selections:
        by_site[row['site']][row['artist_id']] = {'artist_id': row['artist_id'], 'tag': row['tag'], 'display_name': row['display_name']}
    folders, groups = {}, {}
    for site, artists in by_site.items():
        main = db.get_connection()
        try:
            groups[site] = _ensure_group(main, f'{group_prefix} {site}', site, library)
        finally:
            main.close()
        folders.update({(site, a): f for a, f in _ensure_folders(groups[site], site, list(artists.values()), library).items()})
    progress = {'sites': {site: {'total': sum(1 for r in selections if r['site'] == site), 'current': None} for site in by_site},
                'groups': groups, 'log': []}
    conn = connect()
    try:
        delivery_id = conn.execute("INSERT INTO delivery (run_id, status, group_prefix, progress, created_at) VALUES (?, 'queued', ?, ?, ?)",
                                   (run_id, group_prefix, json.dumps(progress), now())).lastrowid
        conn.executemany("INSERT OR IGNORE INTO delivery_item (delivery_id, site, remote_id, artist_id, folder_id, status) VALUES (?,?,?,?,?,'pending')",
                         [(delivery_id, r['site'], r['remote_id'], r['artist_id'], folders[(r['site'], r['artist_id'])]) for r in selections])
        conn.commit()
    finally:
        conn.close()
    return get_delivery(delivery_id)


def resume_delivery(delivery_id: int) -> dict:
    if _task and not _task.done():
        raise RuntimeError('A delivery is already running')
    conn = connect()
    try:
        row = conn.execute('SELECT status FROM delivery WHERE id=?', (delivery_id,)).fetchone()
        if not row:
            raise LookupError('Delivery not found')
        conn.execute("UPDATE delivery_item SET status='pending', error=NULL WHERE delivery_id=? AND status IN ('error','running')", (delivery_id,))
        conn.execute("UPDATE delivery SET status='queued', finished_at=NULL WHERE id=?", (delivery_id,))
        conn.commit()
    finally:
        conn.close()
    return get_delivery(delivery_id)


async def _lookup(provider, site: str, remote_ids: list[str]) -> dict:
    """Fresh metadata keyed by post ID; deleted posts are simply absent."""
    if site == 'gelbooru':
        found = {}
        for remote_id in remote_ids:
            try:
                found[remote_id] = await provider.get_post(remote_id)
            except ValueError:
                continue
        return found
    for attempt in range(TRANSIENT_ATTEMPTS):
        try:
            posts, _ = await provider.search('id:' + ','.join(remote_ids), None, LOOKUP_BATCH, sort='latest')
            return {post.remote_id: post for post in posts}
        except Exception as exc:
            if attempt + 1 == TRANSIENT_ATTEMPTS or not _transient(exc):
                raise
            await asyncio.sleep(2 ** attempt)


def _save(delivery_id: int, progress: dict, status: str | None = None, error: str | None = None) -> None:
    conn = connect()
    try:
        if status:
            finished = now() if status in {'completed', 'canceled', 'failed'} else None
            conn.execute('UPDATE delivery SET status=?, progress=?, error=COALESCE(?, error), finished_at=COALESCE(?, finished_at) WHERE id=?',
                         (status, json.dumps(progress), error, finished, delivery_id))
        else:
            conn.execute('UPDATE delivery SET progress=? WHERE id=?', (json.dumps(progress), delivery_id))
        conn.commit()
    finally:
        conn.close()


def _log(progress: dict, message: str) -> None:
    progress['log'] = (progress.get('log', []) + [f'{now()[:19]} {message}'])[-200:]


def _set_item(delivery_id: int, item, status: str, images: int = 0, error: str | None = None) -> None:
    conn = connect()
    try:
        conn.execute('UPDATE delivery_item SET status=?, images=?, error=? WHERE delivery_id=? AND site=? AND remote_id=? AND artist_id=?',
                     (status, images, error, delivery_id, item['site'], item['remote_id'], item['artist_id']))
        conn.commit()
    finally:
        conn.close()


async def _deliver_site(delivery_id: int, site: str, progress: dict, stop: asyncio.Event, factory: Callable, library: Path) -> None:
    from app.services.images import ImageService
    from app.services.settings import get_parallel_workers
    state = progress['sites'][site]
    try:
        provider = factory(site)
    except ProviderAuthenticationError as exc:
        state['blocked'] = str(exc)
        _log(progress, f'{site}: {exc}')
        return
    provider.literal_query = True
    image_service = ImageService(library)
    semaphore = asyncio.Semaphore(get_parallel_workers())
    filters_cache: dict[int, dict] = {}

    def folder_filters(folder_id: int) -> dict:
        if folder_id not in filters_cache:
            main = db.get_connection()
            try:
                row = main.execute('SELECT filters FROM collection WHERE id=?', (folder_id,)).fetchone()
            finally:
                main.close()
            filters_cache[folder_id] = json.loads(row[0] or '{}') if row else None
        return filters_cache[folder_id]

    async def deliver(item, post) -> None:
        async with semaphore:
            if stop.is_set():
                return
            filters = folder_filters(item['folder_id'])
            if filters is None:
                _set_item(delivery_id, item, 'error', error='Collection was deleted')
                return
            file_progress = {'stage': 'starting', 'remote_id': post.remote_id, 'format': post.format}
            try:
                result = await ingest_post(provider, post, item['folder_id'], filters, image_service, library,
                                           file_progress.update, file_progress)
                _set_item(delivery_id, item, 'done' if result['new_count'] else 'skipped', result['new_count'])
            except StaticPreviewUnavailable as exc:
                _set_item(delivery_id, item, 'filtered', error=str(exc)[:500])
            except Exception as exc:
                emit('planner.delivery_failed', 'Planner delivery item failed', 'ERROR', error=exc, remote_id=item['remote_id'])
                _set_item(delivery_id, item, 'error', error=str(exc)[:500])

    try:
        while not stop.is_set():
            conn = connect()
            try:
                items = [dict(r) for r in conn.execute("""SELECT d.*, a.display_name FROM delivery_item d JOIN artist a ON a.id=d.artist_id
                                                          WHERE d.delivery_id=? AND d.site=? AND d.status='pending'
                                                          ORDER BY d.artist_id, d.remote_id LIMIT ?""", (delivery_id, site, LOOKUP_BATCH))]
            finally:
                conn.close()
            if not items:
                return
            state['current'] = items[0]['display_name']
            try:
                posts = await _lookup(provider, site, list(dict.fromkeys(i['remote_id'] for i in items)))
            except ProviderAuthenticationError as exc:
                state['blocked'] = str(exc)
                _log(progress, f'{site}: {exc}')
                return
            except Exception as exc:
                _log(progress, f'{site}: metadata lookup failed: {exc}')
                for item in items:
                    _set_item(delivery_id, item, 'error', error=f'Metadata lookup failed: {exc}'[:500])
                continue
            for item in items:
                if item['remote_id'] not in posts:
                    _set_item(delivery_id, item, 'missing', error='Post is no longer available')
            await asyncio.gather(*(deliver(item, posts[item['remote_id']]) for item in items if item['remote_id'] in posts))
            _save(delivery_id, progress)
    finally:
        state['current'] = None
        await provider.close()


async def run_delivery(delivery_id: int, stop: asyncio.Event, factory: Callable = create_provider, library: Path | None = None) -> None:
    from app.services.settings import get_processing_settings, processing_context
    library = library or db.LIBRARY_PATH
    delivery = get_delivery(delivery_id)
    progress = delivery['progress']
    processing_context.set(get_processing_settings())
    _log(progress, 'Delivery started')
    _save(delivery_id, progress, 'running')
    status, error = 'completed', None
    try:
        await asyncio.gather(*(_deliver_site(delivery_id, site, progress, stop, factory, library) for site in progress['sites']))
        if stop.is_set():
            status = 'canceled'
    except Exception as exc:
        status, error = 'failed', str(exc)
    _log(progress, f'Delivery {status}')
    _save(delivery_id, progress, status, error)


def launch(delivery_id: int) -> None:
    global _task, _stop
    _stop = asyncio.Event()
    _task = asyncio.get_running_loop().create_task(run_delivery(delivery_id, _stop))


def cancel() -> bool:
    if _task and not _task.done() and _stop:
        _stop.set()
        conn = connect()
        try:
            conn.execute("UPDATE delivery SET status='cancelling' WHERE status='running'")
            conn.commit()
        finally:
            conn.close()
        return True
    return False


async def shutdown() -> None:
    if _task and not _task.done() and _stop:
        _stop.set()
        await asyncio.gather(_task, return_exceptions=True)


POST_URLS = {'danbooru': 'https://danbooru.donmai.us/posts/{}', 'e621': 'https://e621.net/posts/{}',
             'gelbooru': 'https://gelbooru.com/index.php?page=post&s=view&id={}'}


def problems(delivery_id: int, limit: int = 500) -> dict:
    """Posts that did not end as a new image, with the recorded reason."""
    conn = connect()
    try:
        rows = conn.execute("""SELECT d.site, d.remote_id, d.status, d.error, a.display_name FROM delivery_item d
                               JOIN artist a ON a.id=d.artist_id WHERE d.delivery_id=? AND d.status IN ('error','missing','filtered','skipped')
                               ORDER BY CASE d.status WHEN 'error' THEN 0 WHEN 'missing' THEN 1 WHEN 'filtered' THEN 2 ELSE 3 END,
                                        a.display_name LIMIT ?""", (delivery_id, limit)).fetchall()
        return {'items': [{**dict(r), 'url': POST_URLS[r['site']].format(r['remote_id']),
                           'reason': r['error'] or ('Already in the collection (often the same file posted twice)' if r['status'] == 'skipped' else '')}
                          for r in rows]}
    finally:
        conn.close()


def ban_removed_images() -> int:
    """Ban downloaded planner posts whose images were removed from their collection.

    Removing an image in the gallery is a curation decision; without a ban the
    next plan could select the post again and a download would restore it.
    Collections that were deleted entirely are ignored, and locked posts are
    never banned.
    """
    conn = connect()
    try:
        delivered = defaultdict(set)
        for r in conn.execute("SELECT DISTINCT folder_id, artist_id, site, remote_id FROM delivery_item WHERE status IN ('done', 'skipped')"):
            delivered[r['folder_id']].add((r['artist_id'], r['site'], r['remote_id']))
        locked = {(r['artist_id'], r['site'], r['remote_id']) for r in conn.execute("SELECT artist_id, site, remote_id FROM override WHERE action='lock'")}
        banned_already = {(r['artist_id'], r['site'], r['remote_id']) for r in conn.execute("SELECT artist_id, site, remote_id FROM override WHERE action='ban'")}
    finally:
        conn.close()
    removed = []
    main = db.get_connection()
    try:
        for folder_id, posts in delivered.items():
            if not main.execute('SELECT 1 FROM collection WHERE id=?', (folder_id,)).fetchone():
                continue
            present = {(r['provider'], str(r['remote_id']).split(':', 1)[0]) for r in main.execute(
                'SELECT s.provider, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?', (folder_id,))}
            removed += [key for key in posts if (key[1], key[2]) not in present and key not in locked and key not in banned_already]
    finally:
        main.close()
    # Posts accepted from the candidates section are locked; removing their
    # image later turns the lock into a ban.
    conn = connect()
    try:
        accepted = conn.execute("""SELECT p.folder_id, p.artist_id, p.site, p.remote_id, p.seen FROM accepted_post p
                                   JOIN artist a ON a.id=p.artist_id WHERE a.completed_at IS NULL""").fetchall()
    finally:
        conn.close()
    unaccepted = []
    main = db.get_connection()
    try:
        by_folder = defaultdict(list)
        for r in accepted:
            by_folder[r['folder_id']].append(r)
        for folder_id, rows in by_folder.items():
            if not main.execute('SELECT 1 FROM collection WHERE id=?', (folder_id,)).fetchone():
                continue
            present = {(r['provider'], str(r['remote_id']).split(':', 1)[0]) for r in main.execute(
                'SELECT s.provider, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?', (folder_id,))}
            # An accepted post still downloading has no image yet; only act once it was in the folder.
            unaccepted += [r for r in rows if (r['site'], r['remote_id']) not in present
                           and (r['seen'] or main.execute("""SELECT 1 FROM import_batch b JOIN import_item t ON t.batch_id=b.id
                                                WHERE b.collection_id=? AND b.provider=? AND t.remote_id=? AND t.image_id IS NOT NULL""",
                                            (folder_id, r['site'], r['remote_id'])).fetchone())]
    finally:
        main.close()
    if removed or unaccepted:
        conn = connect()
        try:
            conn.executemany("INSERT OR REPLACE INTO override (artist_id, site, remote_id, action, created_at) VALUES (?,?,?,'ban',?)",
                             [(*key, now()) for key in removed] + [(r['artist_id'], r['site'], r['remote_id'], now()) for r in unaccepted])
            conn.executemany('DELETE FROM accepted_post WHERE folder_id=? AND site=? AND remote_id=?',
                             [(r['folder_id'], r['site'], r['remote_id']) for r in unaccepted])
            conn.commit()
        finally:
            conn.close()
    return len(removed) + len(unaccepted)


def stale_images(delivery_id: int) -> dict[int, list[int]]:
    """Images in this delivery's collections that the run no longer selects.

    Only images that an earlier planner delivery put into the collection are
    considered: every source of the image must be a post some delivery
    assigned to that collection, and none may be in this delivery. Manual
    imports and other providers' images are never touched.
    """
    conn = connect()
    try:
        keep, planned = defaultdict(set), defaultdict(set)
        for r in conn.execute('SELECT folder_id, site, remote_id FROM delivery_item WHERE delivery_id=?', (delivery_id,)):
            keep[r['folder_id']].add((r['site'], r['remote_id']))
        if not keep:
            return {}
        marks = ','.join('?' * len(keep))
        for r in conn.execute(f'SELECT DISTINCT folder_id, site, remote_id FROM delivery_item WHERE folder_id IN ({marks})', list(keep)):
            planned[r['folder_id']].add((r['site'], r['remote_id']))
    finally:
        conn.close()
    stale = {}
    main = db.get_connection()
    try:
        for folder_id in keep:
            sources = defaultdict(set)
            for r in main.execute("""SELECT i.id, s.provider, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id
                                     WHERE i.folder_id=?""", (folder_id,)):
                # Motion frames are stored as "<post id>:frame:<n>".
                sources[r['id']].add((r['provider'], str(r['remote_id']).split(':', 1)[0]))
            ids = [image_id for image_id, posts in sources.items()
                   if posts <= planned[folder_id] and not posts & keep[folder_id]]
            if ids:
                stale[folder_id] = sorted(ids)
    finally:
        main.close()
    return stale


def prune_delivery(delivery_id: int, apply: bool, library: Path | None = None) -> dict:
    """Preview, or apply, removing stale images through the recoverable bulk removal.

    Each collection keeps one recovery batch, so applying replaces any earlier
    "Recover last deletion" batch in the affected collections.
    """
    job = get_delivery(delivery_id)
    if not job:
        raise LookupError('Delivery not found')
    if job['status'] in ACTIVE:
        raise RuntimeError('Wait for the download to finish or stop it first')
    stale = stale_images(delivery_id)
    result = {'collections': len(stale), 'images': sum(len(ids) for ids in stale.values()), 'removed': 0}
    if apply and stale:
        import uuid
        from app.services.collections import CollectionService
        service = CollectionService()
        for folder_id, ids in stale.items():
            result['removed'] += len(service.remove_images(folder_id, ids, library or db.LIBRARY_PATH, uuid.uuid4().hex))
    return result


def export_training_layout(delivery_id: int) -> Path:
    """Write each delivered folder's path, image count and repeats for trainers.

    `dataset.toml` uses diffusion-pipe's [[directory]] blocks; folders.json and
    folders.csv carry the same data for other trainers.
    """
    delivery = get_delivery(delivery_id)
    if not delivery:
        raise LookupError('Delivery not found')
    conn = connect()
    try:
        rows = conn.execute("""SELECT DISTINCT d.site, d.artist_id, d.folder_id, a.display_name, a.tag, ra.selected
                               FROM delivery_item d JOIN artist a ON a.id=d.artist_id
                               LEFT JOIN run_artist ra ON ra.run_id=? AND ra.artist_id=d.artist_id
                               WHERE d.delivery_id=? ORDER BY d.site, a.display_name""", (delivery['run_id'], delivery_id)).fetchall()
        config = json.loads(conn.execute('SELECT config FROM run WHERE id=?', (delivery['run_id'],)).fetchone()[0])
    finally:
        conn.close()
    exposures = int(config.get('exposures_per_artist', 200))
    max_repeats = int(config.get('max_repeats', 10))
    main = db.get_connection()
    folders = []
    try:
        for row in rows:
            folder = main.execute('SELECT name, slug FROM collection WHERE id=?', (row['folder_id'],)).fetchone()
            if not folder:
                continue
            images = main.execute('SELECT count(*) FROM image WHERE folder_id=?', (row['folder_id'],)).fetchone()[0]
            # Repeats follow the images left after hand curation, so every
            # artist keeps about the same number of training samples.
            repeats = min(max_repeats, max(1, round(exposures / images))) if images else 0
            folders.append({'site': row['site'], 'artist': row['display_name'], 'tag': row['tag'], 'folder': folder['name'],
                            'path': str((db.LIBRARY_PATH / 'images' / folder['slug']).resolve()), 'images': images,
                            'planned_images': row['selected'], 'repeats': repeats})
    finally:
        main.close()
    target = planner_dir() / 'exports' / f"run-{delivery['run_id']}" / 'training'
    target.mkdir(parents=True, exist_ok=True)
    (target / 'folders.json').write_text(json.dumps(folders, indent=2, ensure_ascii=False), encoding='utf-8')
    with open(target / 'folders.csv', 'w', encoding='utf-8', newline='') as handle:
        import csv
        writer = csv.DictWriter(handle, ['site', 'artist', 'tag', 'folder', 'path', 'images', 'planned_images', 'repeats'], lineterminator='\n')
        writer.writeheader()
        writer.writerows(folders)
    blocks = [f"# Generated by Illustration Scrapping Studio from planner run {delivery['run_id']}.",
              '# Add these [[directory]] blocks to a diffusion-pipe dataset config (resolutions, buckets, etc. go there too).', '']
    for folder in folders:
        if folder['images']:
            blocks += [f"# {folder['site']}: {folder['artist']} ({folder['images']} images)", '[[directory]]',
                       f"path = {json.dumps(folder['path'])}", f"num_repeats = {folder['repeats']}", '']
    (target / 'dataset.toml').write_text('\n'.join(blocks), encoding='utf-8')
    return target
