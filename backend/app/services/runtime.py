"""Single-writer process lease and pre-migration SQLite snapshots (stdlib only)."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os
import sqlite3

class FileLease:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b'0'); self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close(); self.stream = None
            raise RuntimeError('This library/database is already in use. Close the app before backup/restore or starting another backend.') from exc
        return self
    def __exit__(self, *args):
        if self.stream:
            self.stream.close(); self.stream = None

class LibraryLease:
    def __init__(self, library, database):
        self.paths = sorted({Path(library).resolve() / '.collection-studio.lock', Path(str(Path(database).resolve()) + '.lock')})
        self.stack = ExitStack()
    def __enter__(self):
        try:
            for path in self.paths: self.stack.enter_context(FileLease(path))
            return self
        except BaseException:
            self.stack.close(); raise
    def __exit__(self, *args):
        self.stack.close()

def snapshot_database(source, target):
    source, target = Path(source).resolve(), Path(target)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dest:
            src.backup(dest)
            from app.db import register_functions
            register_functions(dest)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database integrity check failed; snapshot was not accepted')

def prepare_migration(database):
    database = Path(database)
    schema = Path(__file__).parents[1] / 'db.py'
    version = hashlib.sha256(schema.read_bytes()).hexdigest()
    marker = database.with_suffix(database.suffix + '.schema-version')
    if database.is_file() and (not marker.exists() or marker.read_text() != version):
        destination = database.parent / 'migration-backups'
        destination.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        snapshot_database(database, destination / f'{database.stem}-{stamp}.sqlite3')
    return marker, version
