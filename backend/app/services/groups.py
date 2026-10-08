"""Artist groups, bounded list imports, and recoverable on-disk folder moves."""
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.db import get_connection
from app.models import FolderCreate
from app.providers.registry import supported_provider
from app.services.queries import humanize_artist_query
from app.services.storage_lock import pair_write


def now():
    return datetime.now(timezone.utc).isoformat()


def folder_slug(name):
    # One safe Windows/Linux component; no traversal, reserved names or trailing dots.
    slug = re.sub(r'[^\w-]+', '-', name.lower().replace('_', '-'), flags=re.UNICODE).strip('-')[:70] or 'artist'
    if re.fullmatch(r'con|prn|aux|nul|com[0-9]|lpt[0-9]', slug):
        slug = 'artist-' + slug
    return slug


def validate_group(conn, group_id):
    if group_id is None:
        return None
    row = conn.execute('SELECT * FROM artist_group WHERE id=?', (group_id,)).fetchone()
    if not row:
        raise ValueError('Group not found')
    return dict(row)


def list_groups():
    conn = get_connection()
    try:
        return [dict(row) for row in conn.execute('SELECT * FROM artist_group ORDER BY name COLLATE NOCASE')]
    finally:
        conn.close()


def create_group(name, provider, library):
    name = name.strip()
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Group name must be 1-100 characters without control characters')
    if not supported_provider(provider):
        raise ValueError('Unsupported group provider')
    # Separate namespace from existing artist folders, stable even after renaming.
    slug = f'{folder_slug(name)}-group-{uuid.uuid4().hex[:8]}'
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        cursor = conn.execute('INSERT INTO artist_group(name,slug,provider,created_at) VALUES(?,?,?,?)', (name, slug, provider, now()))
        for category in ('images', 'thumbnails'):
            (library / category / slug).mkdir(parents=True, exist_ok=False)
        group = validate_group(conn, cursor.lastrowid)
        conn.commit()
        return group
    except Exception:
        conn.rollback()
        for category in ('images', 'thumbnails'):
            try:
                (library / category / slug).rmdir()
            except OSError:
                pass
        raise
    finally:
        conn.close()


@pair_write
def delete_group(group_id, library):
    """Delete members through their normal cleanup path; retain group on failure."""
    from app.services.collections import CollectionService
    conn = get_connection()
    try:
        assert_idle(conn)
        group = conn.execute('SELECT slug FROM artist_group WHERE id=?', (group_id,)).fetchone()
        if not group:
            return False
        members = conn.execute('SELECT id,slug FROM collection WHERE group_id=?', (group_id,)).fetchall()
        for slug in [group['slug'], *(row['slug'] for row in members)]:
            for category in ('images', 'thumbnails'):
                _safe(library / category, slug)
    finally:
        conn.close()
    service = CollectionService()
    for row in members:
        service.delete_collection(row['id'], library)
    # Remove only empty parent directories, never unrelated content.
    for category in ('images', 'thumbnails'):
        path = _safe(library / category, group['slug'])
        if path.exists():
            path.rmdir()
    conn = get_connection()
    try:
        conn.execute('DELETE FROM artist_group WHERE id=?', (group_id,))
        conn.commit()
    finally:
        conn.close()
    return True

def parse_artists(text):
    if len(text.encode('utf-8')) > 1024 * 1024:
        raise ValueError('Artist list exceeds 1 MiB')
    lines = text.lstrip('\ufeff').splitlines()
    if len(lines) > 5000:
        raise ValueError('Use at most 5,000 lines per import')
    seen, entries = set(), []
    for number, raw in enumerate(lines, 1):
        value = humanize_artist_query(raw.strip())
        if not value:
            continue
        # Booru names or profile URLs, one identity per line, never arbitrary search filters.
        invalid = len(value) > 300 or any(ord(c) < 32 for c in value)
        if not value.startswith(('https://', 'http://')):
            invalid = invalid or ':' in value or value.startswith('-')
        key = value.casefold()
        status = 'invalid' if invalid else 'duplicate' if key in seen else 'new'
        entries.append({'line': number, 'artist': value, 'status': status})
        seen.add(key)
    if not entries:
        raise ValueError('The artist list is empty')
    return entries


def import_artists(group_id, text, *, apply=False, library=None, kind="artist", additional_sources=None):
    from app.services.collections import CollectionService
    from app.services.queries import validate_collection_query
    if kind == 'artist':
        entries = parse_artists(text)
    else:
        if len(text.encode('utf-8')) > 1024 * 1024 or len(text.splitlines()) > 5000:
            raise ValueError('Use at most 5,000 lines / 1 MiB')
        entries, seen = [], set()
        for number, raw in enumerate(text.lstrip('\ufeff').splitlines(), 1):
            value = humanize_artist_query(raw) if kind == 'character' else raw.strip()
            if not value:
                continue
            reason = None
            try:
                validate_collection_query(value, kind)
            except ValueError as exc:
                reason = str(exc)
            key = ' '.join(value.split()).casefold()
            entries.append({'line': number, 'artist': value, 'status': 'invalid' if reason else 'duplicate' if key in seen else 'new', 'reason': reason})
            seen.add(key)
        if not entries:
            raise ValueError('The collection list is empty')
    conn = get_connection()
    created_dirs = []
    try:
        if apply:
            conn.execute('BEGIN IMMEDIATE')
        group = validate_group(conn, group_id)
        sources = list(dict.fromkeys([group['provider'], *(additional_sources or [])]))
        for source in sources:
            validate_collection_query('example', kind, source)
        existing = {(humanize_artist_query(row['query']) if kind != 'tag' else ' '.join(row['query'].split())).casefold(): row['id'] for row in conn.execute(
            'SELECT id,query FROM collection WHERE group_id=? AND type=?', (group_id,kind))}
        for entry in entries:
            if entry['status'] == 'new' and ' '.join(entry['artist'].split()).casefold() in existing:
                entry.update(status='existing', folder_id=existing[' '.join(entry['artist'].split()).casefold()])
        if apply and any(e['status'] == 'invalid' for e in entries):
            raise ValueError('Fix invalid lines before importing; nothing was created')
        for entry in entries:
            if apply and entry['status'] == 'new':
                folder = CollectionService().create_collection(FolderCreate(
                    name=entry['artist'], query=entry['artist'], group_id=group_id, type=kind,
                    sources=sources), connection=conn)
                entry.update(status='created', folder_id=folder.id)
        if apply:
            if library is not None:
                for entry in entries:
                    if entry.get('folder_id'):
                        slug = conn.execute('SELECT slug FROM collection WHERE id=?', (entry['folder_id'],)).fetchone()[0]
                        created_dirs.extend(ensure_folder_directories(library, slug))
            conn.commit()
        for entry in entries:
            entry.update(name=entry['artist'], query=entry['artist'], type=kind)
        return {'group_id': group_id, 'provider': group['provider'], 'type': kind, 'items': entries,
                'counts': {status: sum(e['status'] == status for e in entries)
                           for status in ('new', 'existing', 'duplicate', 'invalid', 'created')}}
    except Exception:
        conn.rollback()
        remove_empty_directories(created_dirs)
        raise
    finally:
        conn.close()


def assert_idle(conn):
    # Disk paths must stay stable for all readers/writers with work in flight.
    for table in ('sync_job', 'dataset_job', 'import_batch'):
        if conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone():
            raise ValueError('Finish or cancel pending sync, import and QA jobs before changing group storage or protection')


def _safe(root, relative):
    root = root.resolve()
    parts = relative.replace('\\', '/').split('/')
    if any(part in {'', '.', '..'} or ':' in part for part in parts):
        raise ValueError('Unsafe folder path')
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise ValueError('Folder moves do not follow symlinks or junctions')
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError('Unsafe folder path')
    return path


def remove_empty_directories(paths):
    for path in reversed(paths):
        try:
            path.rmdir()
        except OSError:
            pass


def ensure_folder_directories(library, slug):
    created = []
    try:
        for category in ('images', 'thumbnails'):
            directory = _safe(library / category, slug)
            if not directory.exists():
                directory.mkdir(parents=True)
                created.append(directory)
            elif not directory.is_dir():
                raise ValueError('Folder storage path is occupied by a file')
        return created
    except Exception:
        remove_empty_directories(created)
        raise


def _finish_move(conn, move, library):
    old, new = move['old_slug'], move['new_slug']
    for category in ('images', 'thumbnails'):
        root = library / category
        source, target = _safe(root, old), _safe(root, new)
        if source.exists():
            if target.exists():
                raise ValueError(f'Move target already exists: {target}')
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        else:
            target.mkdir(parents=True, exist_ok=True)
    # Original/training pairs and thumbnails share their folder prefix.
    for row in conn.execute('SELECT id,path,thumb_path FROM image WHERE folder_id=?', (move['folder_id'],)).fetchall():
        def relocated(value):
            if not value:
                return value
            value = value.replace('\\', '/')
            if not value.startswith(old + '/'):
                raise ValueError('Image path is outside its folder; run QA before moving')
            return new + value[len(old):]
        conn.execute('UPDATE image SET path=?,thumb_path=? WHERE id=?',
                     (relocated(row['path']), relocated(row['thumb_path']), row['id']))
    conn.execute('UPDATE collection SET slug=?,group_id=?,updated_at=? WHERE id=?',
                 (new, move['group_id'], now(), move['folder_id']))
    conn.execute('DELETE FROM folder_move WHERE folder_id=?', (move['folder_id'],))
    conn.commit()


@pair_write
def merge_groups(source_id, target_id, name, library, keep_protection=True):
    """Move every collection of one group into another of the same site, rename the result and remove the
    emptied source group. Collections keep their images, captions and planner links; protection from Sync All
    moves with them (or is dropped with keep_protection=False). Checked before anything moves: both groups
    exist, share a site, and have no collection names in common."""
    recover_moves(library)
    name = (name or '').strip()
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise ValueError('Group name must be 1-100 characters without control characters')
    conn = get_connection()
    try:
        assert_idle(conn)
        source, target = validate_group(conn, source_id), validate_group(conn, target_id)
        if source_id == target_id:
            raise ValueError('Drop the group on a different group')
        if source['provider'] != target['provider']:
            raise ValueError(f"Only groups of the same site can be merged ({source['provider']} and {target['provider']})")
        clash = conn.execute("""SELECT s.name FROM collection s JOIN collection t ON t.group_id=? AND t.name=s.name AND t.type=s.type
                                WHERE s.group_id=? LIMIT 5""", (target_id, source_id)).fetchall()
        if clash:
            raise ValueError('Both groups have collections named ' + ', '.join(r['name'] for r in clash) + '; rename or remove one first')
        taken = conn.execute('SELECT 1 FROM artist_group WHERE name=? AND id NOT IN (?, ?)', (name, source_id, target_id)).fetchone()
        if taken:
            raise ValueError(f'Another group is already called "{name}"')
        members = [r['id'] for r in conn.execute('SELECT id FROM collection WHERE group_id=? ORDER BY id', (source_id,))]
        protected = {r['folder_id'] for r in conn.execute('SELECT folder_id FROM group_blocked_folder WHERE group_id=?', (source_id,))}
    finally:
        conn.close()
    moved = 0
    for folder_id in members:
        conn = get_connection()
        try:
            conn.execute('DELETE FROM group_blocked_folder WHERE folder_id=?', (folder_id,))
            conn.commit()
        finally:
            conn.close()
        try:
            move_folder(folder_id, target_id, library)
        except Exception as exc:
            conn = get_connection()
            try:  # put the protection back on the folder that could not move
                if folder_id in protected:
                    conn.execute('INSERT OR IGNORE INTO group_blocked_folder(group_id, folder_id, created_at) VALUES (?,?,?)', (source_id, folder_id, now()))
                    conn.commit()
            finally:
                conn.close()
            raise ValueError(f'Merge stopped after {moved} of {len(members)} collections: {exc}') from exc
        if folder_id in protected and keep_protection:
            conn = get_connection()
            try:
                conn.execute('INSERT OR IGNORE INTO group_blocked_folder(group_id, folder_id, created_at) VALUES (?,?,?)', (target_id, folder_id, now()))
                conn.commit()
            finally:
                conn.close()
        moved += 1
    delete_group(source_id, library)
    conn = get_connection()
    try:
        conn.execute('UPDATE artist_group SET name=? WHERE id=?', (name, target_id))
        conn.commit()
        return {'group': validate_group(conn, target_id), 'moved': moved, 'protected': len(protected) if keep_protection else 0}
    finally:
        conn.close()


@pair_write
def recover_moves(library):
    conn = get_connection()
    try:
        for row in conn.execute('SELECT * FROM folder_move').fetchall():
            _finish_move(conn, dict(row), library)
    finally:
        conn.close()


@pair_write
def move_folder(folder_id, group_id, library):
    recover_moves(library)
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        assert_idle(conn)
        group = validate_group(conn, group_id)
        folder = conn.execute('SELECT * FROM collection WHERE id=?', (folder_id,)).fetchone()
        if not folder:
            raise ValueError('Folder not found')
        if folder['group_id'] == group_id:
            return
        if conn.execute('SELECT 1 FROM group_blocked_folder WHERE folder_id=?', (folder_id,)).fetchone():
            raise ValueError('Unprotect this collection before moving it, then protect it in the destination group if needed')
        if conn.execute('SELECT 1 FROM collection WHERE group_id IS ? AND name=? AND type=? AND id<>?', (group_id, folder['name'], folder['type'], folder_id)).fetchone():
            raise ValueError('A folder with this name already exists in the destination group')
        old = folder['slug']
        leaf = Path(old).name
        base = f"{group['slug']}/{leaf}" if group else leaf
        new, suffix = base, 1
        while conn.execute('SELECT 1 FROM collection WHERE lower(slug)=lower(?)', (new,)).fetchone() or any((library / kind / new).exists() for kind in ('images', 'thumbnails')):
            new, suffix = f'{base}-{suffix}', suffix + 1
        for kind in ('images', 'thumbnails'):
            _safe(library / kind, old)
            _safe(library / kind, new)
        for row in conn.execute('SELECT path,thumb_path FROM image WHERE folder_id=?', (folder_id,)):
            if any(value and not value.replace('\\', '/').startswith(old + '/') for value in row):
                raise ValueError('Image path is outside its folder; run QA before moving')
        move = dict(folder_id=folder_id, old_slug=old, new_slug=new, group_id=group_id, created_at=now())
        conn.execute('INSERT INTO folder_move VALUES(:folder_id,:old_slug,:new_slug,:group_id,:created_at)', move)
        conn.commit()  # Journal first: startup finishes interrupted directory moves before reconciliation.
        _finish_move(conn, move, library)
    finally:
        conn.close()
