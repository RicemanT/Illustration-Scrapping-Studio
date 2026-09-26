"""Initialize the current schema; normal studio.py startup already does this."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'backend'))

if __name__ == '__main__':
    from app.db import DB_PATH, LIBRARY_PATH, init_db
    from app.services.runtime import LibraryLease, prepare_migration
    with LibraryLease(LIBRARY_PATH, DB_PATH):
        marker, version = prepare_migration(DB_PATH)
        init_db()
        marker.write_text(version)
    print(f'Database initialized: {DB_PATH}')
