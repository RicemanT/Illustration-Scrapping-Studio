"""Offline portable library archives. No overwrite/merge restore mode."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile
from app.services.runtime import LibraryLease, snapshot_database


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): value.update(chunk)
    return value.hexdigest()


def safe_name(name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or '\\' in name or ':' in name or any(part in ('.', '..') or part.endswith((' ', '.')) for part in parts):
        raise ValueError('Unsafe archive path')
    if PurePosixPath(name).as_posix() != name:
        raise ValueError('Noncanonical archive path')
    if parts[0] != 'library' or len(parts) < 2:
        raise ValueError('Archive entry must be inside library/')
    for part in parts:
        stem = part.split('.')[0].casefold()
        if stem in {'con','prn','aux','nul', *[f'com{i}' for i in range(10)], *[f'lpt{i}' for i in range(10)]}:
            raise ValueError('Reserved archive filename')
    return parts[1:]


def backup(library, database, destination):
    library, database, destination = map(lambda p: Path(p).resolve(), (library, database, destination))
    if destination == library or library in destination.parents:
        raise ValueError('Store backups outside the library')
    if not database.is_file(): raise ValueError('Library database does not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with LibraryLease(library, database), tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / 'index.db'
        snapshot_database(database, snapshot)
        files = {'library/index.db': snapshot}
        for path in library.rglob('*'):
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError(f'Backup does not follow links: {path}')
            if not path.is_file(): continue
            relative = path.relative_to(library)
            if relative.parts[0] in {'migration-backups', '.scratch'} or path == database or path.name in {'index.db', 'index.db-wal', 'index.db-shm'} or path.name.endswith(('.lock', '.schema-version')) or str(path) in {str(database)+'-wal',str(database)+'-shm'}:
                continue
            name = 'library/' + relative.as_posix()
            safe_name(name)
            files[name] = path
        external_settings = os.getenv('ARTIST_PROVIDER_SETTINGS_PATH')
        if external_settings and Path(external_settings).is_file():
            files['library/provider_settings.json'] = Path(external_settings)
        manifest = {'format': 'image-collection-studio-library', 'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'contains_private_settings': True, 'files': {}}
        with destination.open('xb') as output:
            try:
                with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                    for name, path in files.items():
                        manifest['files'][name] = {'size': path.stat().st_size, 'sha256': digest(path)}
                        archive.write(path, name)
                    archive.writestr('manifest.json', json.dumps(manifest, indent=2))
            except BaseException:
                output.close(); destination.unlink(missing_ok=True); raise
    return {'archive': str(destination), 'files': len(files), 'sha256': digest(destination)}


def restore(archive_path, destination):
    destination = Path(destination).resolve()
    if destination.exists(): raise ValueError('Restore requires a NEW directory; existing libraries are never overwritten')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.collection-restore-', dir=destination.parent)).resolve()
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(n.casefold() for n in names)):
                raise ValueError('Duplicate archive paths')
            if archive.getinfo('manifest.json').file_size > 64 * 1024 * 1024:
                raise ValueError('Manifest is too large')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('format') != 'image-collection-studio-library' or manifest.get('version') != 1:
                raise ValueError('Unsupported archive format')
            records = manifest['files']
            if 'library/index.db' not in records or set(names) != set(records) | {'manifest.json'}:
                raise ValueError('Archive does not match its manifest')
            total = sum(archive.getinfo(name).file_size for name in records)
            if total > shutil.disk_usage(destination.parent).free:
                raise ValueError('Not enough free space for restored library')
            for name, record in records.items():
                parts = safe_name(name)
                info = archive.getinfo(name)
                if info.file_size != record['size'] or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Invalid archive file metadata')
                target = staging.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if digest(target) != record['sha256']:
                    raise ValueError(f'Checksum mismatch: {name}')
        with closing(sqlite3.connect(staging / 'index.db')) as conn:
            from app.db import register_functions
            register_functions(conn)
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Restored database failed integrity check')
            if conn.execute('PRAGMA foreign_key_check').fetchone(): raise ValueError('Restored database has broken references')
            # Restoring must never unexpectedly restart scraping/scheduled writes.
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'collection' not in tables or 'image' not in tables: raise ValueError('Not a collection database')
            for table in ('sync_job', 'dataset_job', 'import_batch'):
                if table in tables: conn.execute(f"UPDATE {table} SET status='canceled' WHERE status IN ('queued','running','cancelling')")
            if 'sync_schedule' in tables: conn.execute('UPDATE sync_schedule SET enabled=0, next_run_at=NULL')
            conn.commit()
            slugs = [row[0] for row in conn.execute('SELECT slug FROM collection')]
            if 'artist_group' in tables: slugs.extend(row[0] for row in conn.execute('SELECT slug FROM artist_group'))
            for slug in slugs:
                for category in ('images', 'thumbnails'):
                    parts = safe_name('library/' + category + '/' + slug)
                    staging.joinpath(*parts).mkdir(parents=True, exist_ok=True)
        if destination.exists(): raise ValueError('Restore destination appeared during verification')
        staging.rename(destination)
        return {'library': str(destination), 'files': len(records), 'scheduled_scraping': 'disabled'}
    finally:
        # Delete only this operation's checked temporary sibling, never the destination.
        if staging.exists() and staging.parent == destination.parent and staging.name.startswith('.collection-restore-'):
            shutil.rmtree(staging)
