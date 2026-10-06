import sqlite3
import os
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DEFAULT_LIBRARY_PATH = Path(__file__).parent.parent.parent / "library"
LIBRARY_PATH = Path(os.getenv("ARTIST_LIBRARY_PATH", str(_DEFAULT_LIBRARY_PATH))).expanduser().resolve()
DB_PATH = Path(os.getenv("ARTIST_DB_PATH", str(LIBRARY_PATH / "index.db"))).expanduser().resolve()


def get_connection() -> sqlite3.Connection:
    """Get a database connection with proper configuration."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # WAL allows readers (gallery/list endpoints) to continue while a short
    # collection/import write is committing. The timeout absorbs brief overlap
    # between background sync/import jobs instead of surfacing "database is
    # locked" to the user.
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    register_functions(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def register_functions(conn):
    from app.services.tags import normalize_tag
    conn.create_function('tag_normalize', 1, lambda value: normalize_tag(value or ''), deterministic=True)
    conn.create_function('tag_key', 1, lambda value: normalize_tag(value or '').casefold(), deterministic=True)
    conn.create_function('path_stem', 1, lambda value: str(Path((value or '').replace('\\', '/')).with_suffix('')).casefold() if value else '', deterministic=True)


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    """Apply a small idempotent migration to an existing SQLite database."""
    columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_minimum_training_dimensions(cursor: sqlite3.Cursor) -> None:
    """Apply the universal 512px floor and replace the former 768px default."""
    now = datetime.now(timezone.utc).isoformat()
    for row in cursor.execute("SELECT id, filters FROM collection").fetchall():
        try:
            filters = json.loads(row[1] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(filters, dict):
            continue
        changed = False
        for key in ("min_width", "min_height"):
            try:
                current = int(filters.get(key, 512))
            except (TypeError, ValueError):
                current = 512
            replacement = 512 if current == 768 or current < 512 else current
            if filters.get(key) != replacement:
                filters[key] = replacement
                changed = True
        if changed:
            cursor.execute(
                "UPDATE collection SET filters = ?, updated_at = ? WHERE id = ?",
                (json.dumps(filters), now, row[0]),
            )


def _migrate_human_readable_artist_queries(cursor: sqlite3.Cursor) -> None:
    """Remove obsolete provider underscores from simple stored artist names."""
    now = datetime.now(timezone.utc).isoformat()
    rows = cursor.execute(
        "SELECT id, query FROM collection WHERE type = 'artist' AND query LIKE '%_%'"
    ).fetchall()
    for row in rows:
        if ":" in row[1]:
            continue
        readable = " ".join(row[1].replace("_", " ").split())
        cursor.execute(
            "UPDATE collection SET query = ?, updated_at = ? WHERE id = ?",
            (readable, now, row[0]),
        )


def _migrate_e621_species_category(cursor: sqlite3.Cursor) -> None:
    """Promote e621 species metadata into its new trainer-facing category."""
    if cursor.execute(
        "SELECT 1 FROM app_setting WHERE key = 'ground_truth_species_v1'"
    ).fetchone():
        return

    now = datetime.now(timezone.utc).isoformat()

    def include_species(raw: str) -> str:
        try:
            categories = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            categories = []
        if not isinstance(categories, list):
            categories = []
        categories = [str(category) for category in categories]
        if "species" not in categories:
            insertion_point = next(
                (categories.index(category) for category in ("general", "meta") if category in categories),
                len(categories),
            )
            categories.insert(insertion_point, "species")
        return json.dumps(categories)

    global_row = cursor.execute(
        "SELECT value FROM app_setting WHERE key = 'ground_truth_categories'"
    ).fetchone()
    if global_row:
        cursor.execute(
            "UPDATE app_setting SET value = ?, updated_at = ? WHERE key = 'ground_truth_categories'",
            (include_species(global_row[0]), now),
        )
    for row in cursor.execute(
        "SELECT id, ground_truth_categories FROM collection WHERE ground_truth_categories IS NOT NULL"
    ).fetchall():
        cursor.execute(
            "UPDATE collection SET ground_truth_categories = ?, updated_at = ? WHERE id = ?",
            (include_species(row[1]), now, row[0]),
        )

    for source in cursor.execute(
        "SELECT id, image_id, metadata FROM image_source WHERE provider = 'e621'"
    ).fetchall():
        try:
            metadata = json.loads(source[2] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        tags = metadata.get("tags", {}) if isinstance(metadata, dict) else {}
        species_tags = tags.get("species", []) if isinstance(tags, dict) else []
        if isinstance(species_tags, str):
            species_tags = species_tags.split()
        if not isinstance(species_tags, list):
            continue
        for tag in species_tags:
            tag = str(tag).strip()
            if not tag:
                continue
            exists = cursor.execute(
                """SELECT 1 FROM image_tag
                   WHERE image_id = ? AND source_id = ? AND category = 'species' AND tag = ?
                   LIMIT 1""",
                (source[1], source[0], tag),
            ).fetchone()
            if not exists:
                cursor.execute(
                    """INSERT INTO image_tag (image_id, source_id, category, tag)
                       VALUES (?, ?, 'species', ?)""",
                    (source[1], source[0], tag),
                )

    cursor.execute(
        "INSERT INTO app_setting (key, value, updated_at) VALUES ('ground_truth_species_v1', 'migrated', ?)",
        (now,),
    )


def _migrate_folder_scoped_images(conn: sqlite3.Connection) -> None:
    """Replace the legacy library-wide SHA uniqueness with folder ownership."""
    sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'image'").fetchone()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(image)")}
    sql = (sql_row[0] if sql_row else "").lower().replace(" ", "")
    if "folder_id" in columns and "sha256textunique" not in sql:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE image_folder_scoped (
            id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL,
            md5 TEXT,
            phash INTEGER,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            format TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            path TEXT NOT NULL,
            thumb_path TEXT,
            added_at TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            favorite INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            dhash INTEGER,
            gradient_hash TEXT,
            colorhash TEXT,
            derived_from_preview INTEGER NOT NULL DEFAULT 0,
            original_media_format TEXT,
            original_media_url TEXT,
            derived_media_source TEXT,
            folder_id INTEGER,
            UNIQUE (folder_id, sha256),
            FOREIGN KEY (folder_id) REFERENCES collection(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        INSERT INTO image_folder_scoped (
            id, sha256, md5, phash, width, height, format, file_size, path,
            thumb_path, added_at, review_status, favorite, notes, dhash,
            gradient_hash, colorhash, derived_from_preview,
            original_media_format, original_media_url, derived_media_source,
            folder_id
        )
        SELECT i.id, i.sha256, i.md5, i.phash, i.width, i.height, i.format,
               i.file_size, i.path, i.thumb_path, i.added_at, i.review_status,
               i.favorite, i.notes, i.dhash, i.gradient_hash, i.colorhash,
               i.derived_from_preview, i.original_media_format,
               i.original_media_url, i.derived_media_source,
               COALESCE(
                   (SELECT MIN(ci.collection_id) FROM collection_image ci WHERE ci.image_id = i.id),
                   (SELECT c.id FROM collection c
                    WHERE LOWER(c.slug) = LOWER(SUBSTR(REPLACE(i.path, '\\', '/'), 1,
                        INSTR(REPLACE(i.path, '\\', '/'), '/') - 1)) LIMIT 1)
               )
        FROM image i
    """)
    conn.execute("DROP TABLE image")
    conn.execute("ALTER TABLE image_folder_scoped RENAME TO image")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_folder_scoped_sources(conn: sqlite3.Connection) -> None:
    """Allow the same remote post to have independent provenance per folder."""
    sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'image_source'").fetchone()
    sql = (sql_row[0] if sql_row else "").lower().replace(" ", "")
    if "unique(image_id,provider,remote_id,version)" in sql:
        return
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE image_source_folder_scoped (
            id INTEGER PRIMARY KEY,
            image_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            remote_url TEXT,
            fetched_at TEXT NOT NULL,
            metadata TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            source_url TEXT,
            metadata_version INTEGER NOT NULL DEFAULT 1,
            is_primary INTEGER NOT NULL DEFAULT 0,
            UNIQUE (image_id, provider, remote_id, version),
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        INSERT INTO image_source_folder_scoped
        SELECT id, image_id, provider, remote_id, remote_url, fetched_at,
               metadata, version, source_url, metadata_version, is_primary
        FROM image_source
    """)
    conn.execute("DROP TABLE image_source")
    conn.execute("ALTER TABLE image_source_folder_scoped RENAME TO image_source")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_group_folders(conn):
    """Preserve folder IDs and every column while allowing names in separate groups."""
    conn.execute("""CREATE TABLE IF NOT EXISTS artist_group (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        slug TEXT NOT NULL UNIQUE, provider TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    _ensure_column(conn.cursor(), 'collection', 'group_id',
                   'INTEGER REFERENCES artist_group(id) ON DELETE RESTRICT')
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='collection'").fetchone()[0]
    if 'name TEXT UNIQUE NOT NULL' in schema:
        indexes = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='collection' AND sql IS NOT NULL AND type IN ('index','trigger')").fetchall()
        conn.commit()
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('PRAGMA legacy_alter_table=ON')
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(schema.replace('CREATE TABLE collection', 'CREATE TABLE collection_grouped', 1)
                         .replace('name TEXT UNIQUE NOT NULL', 'name TEXT NOT NULL', 1))
            conn.execute('INSERT INTO collection_grouped SELECT * FROM collection')
            conn.execute('DROP TABLE collection')
            conn.execute('ALTER TABLE collection_grouped RENAME TO collection')
            for row in indexes:
                conn.execute(row[0])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute('PRAGMA legacy_alter_table=OFF')
            conn.execute('PRAGMA foreign_keys=ON')
    # Exact legacy names remain valid; bulk artist identity uses normalized queries.
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_group_type_name ON collection(COALESCE(group_id, 0), type, name)')
    conn.execute('DROP INDEX IF EXISTS idx_folder_group_name')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_folder_group ON collection(group_id)')
    conn.execute("""CREATE TABLE IF NOT EXISTS folder_move (
        folder_id INTEGER PRIMARY KEY REFERENCES collection(id) ON DELETE RESTRICT,
        old_slug TEXT NOT NULL, new_slug TEXT NOT NULL, group_id INTEGER,
        created_at TEXT NOT NULL
    )""")


def init_db():
    """Initialize the database schema."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL")

    # Collections table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collection (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL,
            query TEXT NOT NULL,
            caption_template TEXT NOT NULL,
            filters TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_sync_at TEXT,
            enabled INTEGER DEFAULT 1
        )
    """)

    _migrate_group_folders(conn)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS group_blocked_folder (
            group_id INTEGER NOT NULL,
            folder_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(group_id, folder_id),
            FOREIGN KEY(group_id) REFERENCES artist_group(id) ON DELETE CASCADE,
            FOREIGN KEY(folder_id) REFERENCES collection(id) ON DELETE CASCADE
        )
    """)

    # Collection sources table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collection_source (
            id INTEGER PRIMARY KEY,
            collection_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            last_cursor TEXT,
            backfill_cursor TEXT,
            enabled INTEGER DEFAULT 1,
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
            UNIQUE (collection_id, provider)
        )
    """)

    # Images table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS image (
            id INTEGER PRIMARY KEY,
            sha256 TEXT NOT NULL,
            md5 TEXT,
            phash INTEGER,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            format TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            path TEXT NOT NULL,
            thumb_path TEXT,
            added_at TEXT NOT NULL,
            folder_id INTEGER,
            UNIQUE (folder_id, sha256),
            FOREIGN KEY (folder_id) REFERENCES collection(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_md5 ON image(md5)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_phash ON image(phash)")
    _ensure_column(cursor, "image", "processing_policy", "TEXT")
    _migrate_human_readable_artist_queries(cursor)
    _migrate_minimum_training_dimensions(cursor)

    # Legacy folder membership table. image.folder_id is the canonical owner;
    # this table remains for review state and non-destructive DB compatibility.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collection_image (
            collection_id INTEGER NOT NULL,
            image_id INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, image_id),
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_collection_image_collection ON collection_image(collection_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_collection_image_image ON collection_image(image_id)")
    # Image sources table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS image_source (
            id INTEGER PRIMARY KEY,
            image_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            remote_url TEXT,
            fetched_at TEXT NOT NULL,
            metadata TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            UNIQUE (image_id, provider, remote_id, version),
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_provider_remote ON image_source(provider, remote_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_image ON image_source(image_id)")

    # Image tags table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS image_tag (
            image_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            tag TEXT NOT NULL,
            score INTEGER,
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE,
            FOREIGN KEY (source_id) REFERENCES image_source(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_image ON image_tag(image_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_category_tag ON image_tag(category, tag)")

    # Rejected posts table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rejected_post (
            provider TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            rejected_at TEXT NOT NULL,
            reason TEXT,
            PRIMARY KEY (provider, remote_id)
        )
    """)

    # Collection captions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collection_caption (
            collection_id INTEGER NOT NULL,
            image_id INTEGER NOT NULL,
            caption TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            template_version INTEGER NOT NULL,
            PRIMARY KEY (collection_id, image_id),
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
        )
    """)

    # Phase 1 review/provenance migrations for databases created by earlier builds.
    _ensure_column(cursor, "image", "review_status", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(cursor, "image", "favorite", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "image", "notes", "TEXT")
    _ensure_column(cursor, "image", "dhash", "INTEGER")
    _ensure_column(cursor, "image", "gradient_hash", "TEXT")
    _ensure_column(cursor, "image", "colorhash", "TEXT")
    _ensure_column(cursor, "image", "derived_from_preview", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(cursor, "image", "original_media_format", "TEXT")
    _ensure_column(cursor, "image", "original_media_url", "TEXT")
    _ensure_column(cursor, "image", "derived_media_source", "TEXT")
    # Hand-assigned quality and aesthetic marks. quality_tags (JSON list) is
    # what was committed to the ground truth when the folder was accepted.
    _ensure_column(cursor, "image", "quality_mark", "TEXT")
    _ensure_column(cursor, "image", "aesthetic_mark", "TEXT")
    _ensure_column(cursor, "image", "marks_viewed_at", "TEXT")
    _ensure_column(cursor, "image", "quality_tags", "TEXT")
    # quality_source: "manual" (set by hand, never overwritten) or "auto" (from score percentiles).
    _ensure_column(cursor, "image", "quality_source", "TEXT")
    _ensure_column(cursor, "image", "quality_auto", "TEXT")
    _ensure_column(cursor, "image", "quality_auto_info", "TEXT")
    # When the post was originally published on its site (UTC ISO), for era sorting.
    _ensure_column(cursor, "image", "posted_at", "TEXT")
    # Aesthetic marks from the analysis scorers, kept apart from hand-set ones like quality marks.
    _ensure_column(cursor, "image", "aesthetic_source", "TEXT")
    _ensure_column(cursor, "image", "aesthetic_auto", "TEXT")
    _ensure_column(cursor, "image", "aesthetic_auto_info", "TEXT")
    # Review flags from the image analysis (JSON list), for flags-first review.
    _ensure_column(cursor, "image", "analysis_flags", "TEXT")
    _ensure_column(cursor, "image", "analysis_flag_count", "INTEGER")
    cursor.execute("UPDATE image SET aesthetic_source = 'manual' WHERE aesthetic_mark IS NOT NULL AND aesthetic_source IS NULL")
    cursor.execute("UPDATE image SET quality_source = 'manual' WHERE quality_mark IS NOT NULL AND quality_source IS NULL")
    _migrate_folder_scoped_images(conn)
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_md5 ON image(md5)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_phash ON image(phash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_folder ON image(folder_id)")
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS collection_image_folder_guard
        BEFORE INSERT ON collection_image
        WHEN (SELECT folder_id FROM image WHERE id = NEW.image_id) IS NOT NEW.collection_id
        BEGIN
            SELECT RAISE(ABORT, 'image membership must match its owning folder');
        END
    """)
    # Rows made by the first preview-fallback build are known provider
    # thumbnails. Keep the old boolean for compatibility while making the
    # origin explicit for new UI/API clients.
    cursor.execute("""
        UPDATE image SET derived_media_source = 'provider_preview'
        WHERE derived_from_preview = 1 AND derived_media_source IS NULL
    """)
    _ensure_column(cursor, "collection_image", "review_status", "TEXT NOT NULL DEFAULT 'pending'")
    _ensure_column(cursor, "collection_image", "selected", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(cursor, "collection_image", "rejection_reason", "TEXT")
    _ensure_column(cursor, "image_source", "source_url", "TEXT")
    _ensure_column(cursor, "image_source", "metadata_version", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(cursor, "image_source", "is_primary", "INTEGER NOT NULL DEFAULT 0")
    _migrate_folder_scoped_sources(conn)
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_provider_remote ON image_source(provider, remote_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_image ON image_source(image_id)")
    cursor.execute("UPDATE image_source SET source_url = remote_url WHERE source_url IS NULL")
    cursor.execute("UPDATE image_source SET metadata_version = version WHERE metadata_version <> version")
    # Backfill one stable primary source identity per image. All metadata
    # snapshots for that identity remain primary; later providers are still
    # retained as complete provenance records.
    cursor.execute("""
        UPDATE image_source SET is_primary = 1
        WHERE provider <> 'filesystem' AND (image_id, provider, remote_id) IN (
            SELECT first_source.image_id, first_source.provider, first_source.remote_id
            FROM image_source AS first_source
            WHERE first_source.id = (
                SELECT MIN(candidate.id) FROM image_source AS candidate
                WHERE candidate.image_id = first_source.image_id
                  AND candidate.provider <> 'filesystem'
            )
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS import_batch (
            id INTEGER PRIMARY KEY,
            collection_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            job_id TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS import_item (
            id INTEGER PRIMARY KEY,
            batch_id INTEGER NOT NULL,
            remote_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            image_id INTEGER,
            error TEXT,
            FOREIGN KEY (batch_id) REFERENCES import_batch(id) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE SET NULL,
            UNIQUE (batch_id, remote_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_import_item_batch ON import_item(batch_id)")

    # Import jobs were initially kept only in memory. Keep the extra state in
    # SQLite so the batch can still be inspected after a process restart.
    _ensure_column(cursor, "import_batch", "started_at", "TEXT")
    _ensure_column(cursor, "import_batch", "completed_at", "TEXT")
    _ensure_column(cursor, "import_batch", "error", "TEXT")
    _ensure_column(cursor, "import_batch", "job_id", "TEXT")
    _ensure_column(cursor, "import_batch", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_candidate (
            id INTEGER PRIMARY KEY,
            image_id_a INTEGER NOT NULL,
            image_id_b INTEGER NOT NULL,
            phash_distance INTEGER,
            dhash_distance INTEGER,
            color_distance INTEGER,
            visual_similarity REAL NOT NULL,
            methods TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE (image_id_a, image_id_b),
            CHECK (image_id_a < image_id_b),
            FOREIGN KEY (image_id_a) REFERENCES image(id) ON DELETE CASCADE,
            FOREIGN KEY (image_id_b) REFERENCES image(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_candidate_status ON duplicate_candidate(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_candidate_a ON duplicate_candidate(image_id_a)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_candidate_b ON duplicate_candidate(image_id_b)")
    _ensure_column(cursor, "duplicate_candidate", "gradient_distance", "INTEGER")
    _ensure_column(cursor, "duplicate_candidate", "aspect_similarity", "REAL")
    _ensure_column(cursor, "duplicate_candidate", "resolved_action", "TEXT")
    _ensure_column(cursor, "duplicate_candidate", "kept_image_id", "INTEGER")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_dhash ON image(dhash)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_gradient_hash ON image(gradient_hash)")

    # Keep an audit trail after a merge removes its candidate/image rows.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_resolution (
            id INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            kept_image_id INTEGER,
            removed_image_id INTEGER,
            kept_sha256 TEXT,
            removed_sha256 TEXT,
            details TEXT NOT NULL,
            resolved_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_duplicate_resolution_candidate ON duplicate_resolution(candidate_id)")

    # User-curated training tags are stored as overrides rather than changing
    # immutable provider tag/provenance rows.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS image_tag_override (
            image_id INTEGER NOT NULL,
            tag TEXT NOT NULL COLLATE NOCASE,
            action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
            category TEXT NOT NULL DEFAULT 'general',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (image_id, tag),
            FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_override_image ON image_tag_override(image_id)")
    _ensure_column(cursor, "image_tag_override", "position", "INTEGER")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tag_edit_operation (
            id INTEGER PRIMARY KEY,
            token TEXT UNIQUE NOT NULL,
            collection_id INTEGER,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            undone_at TEXT,
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE SET NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_edit_operation_token ON tag_edit_operation(token)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_setting (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute(
        """INSERT OR IGNORE INTO app_setting (key, value, updated_at)
           VALUES ('ground_truth_categories', '[\"artist\",\"character\",\"copyright\",\"species\",\"general\"]', ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    cursor.execute(
        """INSERT OR IGNORE INTO app_setting (key, value, updated_at)
           VALUES ('parallel_workers', '2', ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    _ensure_column(cursor, "collection_source", "backfill_cursor", "TEXT")
    _ensure_column(cursor, "collection_source", "query_override", "TEXT")
    cursor_migration = cursor.execute(
        "SELECT 1 FROM app_setting WHERE key = 'sync_cursor_v2'"
    ).fetchone()
    if not cursor_migration:
        # Earlier builds used last_cursor as an older-page pagination token.
        # Preserve it as the backfill position and reserve last_cursor for the
        # newest successfully handled remote ID (the incremental watermark).
        cursor.execute(
            "UPDATE collection_source SET backfill_cursor = last_cursor, last_cursor = NULL WHERE last_cursor IS NOT NULL"
        )
        cursor.execute(
            "INSERT INTO app_setting (key, value, updated_at) VALUES ('sync_cursor_v2', 'migrated', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
    _ensure_column(cursor, "collection", "ground_truth_categories", "TEXT")
    _migrate_e621_species_category(cursor)
    # Existing folders inherit their old, unformatted behavior. New folders
    # explicitly store the user's artist tag format at creation time.
    _ensure_column(cursor, "collection", "artist_tag_template", "TEXT")
    # Curation era: images posted before this year are outside the style kept for training.
    _ensure_column(cursor, "collection", "era_from", "INTEGER")

    # Phase 4 exports are immutable, versioned snapshots. Validation itself is
    # deliberately read-only and is computed on demand; only completed/failed
    # export records are persisted for later inspection.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dataset_export (
            id TEXT PRIMARY KEY,
            collection_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            output_path TEXT,
            manifest_path TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            image_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            result TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dataset_export_collection ON dataset_export(collection_id, created_at)")

    # Phase 5 sync jobs are durable so progress/history survives a backend
    # restart. Interrupted jobs can safely replay their uncommitted cursor page.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_job (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('folder', 'all')),
            trigger TEXT NOT NULL DEFAULT 'manual' CHECK (trigger IN ('manual', 'scheduled', 'resumed')),
            collection_id INTEGER,
            provider TEXT,
            status TEXT NOT NULL,
            parameters TEXT NOT NULL DEFAULT '{}',
            progress TEXT NOT NULL DEFAULT '{}',
            logs TEXT NOT NULL DEFAULT '[]',
            result TEXT,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE SET NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_job_created ON sync_job(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_job_status ON sync_job(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_folder_added_id ON image(folder_id, added_at, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_source_latest ON image_source(image_id, provider, remote_id, version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_source ON image_tag(source_id, image_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_image_key ON image_tag(image_id,tag_key(tag),category,source_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_override_image_key ON image_tag_override(image_id,tag_key(tag),action)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_key_image ON image_tag(tag_key(tag),image_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_override_key_image ON image_tag_override(tag_key(tag),image_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_image_folder_stem ON image(folder_id,path_stem(path))")
    # Dataset work uses the shared durable job serializer, but a separate
    # queue so sync restart recovery cannot accidentally scrape for a QA job.
    cursor.execute("""CREATE TABLE IF NOT EXISTS dataset_job (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT 'manual',
        collection_id INTEGER, provider TEXT, status TEXT NOT NULL,
        parameters TEXT NOT NULL DEFAULT '{}', progress TEXT NOT NULL DEFAULT '{}',
        logs TEXT NOT NULL DEFAULT '[]', result TEXT, error TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        started_at TEXT, finished_at TEXT,
        FOREIGN KEY(collection_id) REFERENCES collection(id) ON DELETE SET NULL)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS dataset_job_issue (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, image_id INTEGER, payload TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES dataset_job(id) ON DELETE CASCADE)""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dataset_issue_job ON dataset_job_issue(job_id,id)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS dataset_job_target (
        job_id TEXT NOT NULL, image_id INTEGER NOT NULL, PRIMARY KEY(job_id,image_id),
        FOREIGN KEY(job_id) REFERENCES dataset_job(id) ON DELETE CASCADE)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS filter_review (
        token TEXT PRIMARY KEY, folder_id INTEGER NOT NULL, payload TEXT NOT NULL,
        created_at TEXT NOT NULL, applied_at TEXT, undone_at TEXT,
        FOREIGN KEY(folder_id) REFERENCES collection(id) ON DELETE CASCADE)""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_schedule (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0,
            interval_minutes INTEGER NOT NULL DEFAULT 1440,
            limit_per_source INTEGER NOT NULL DEFAULT 20,
            sort TEXT NOT NULL DEFAULT 'latest',
            last_run_at TEXT,
            next_run_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    cursor.execute(
        """INSERT OR IGNORE INTO sync_schedule
           (id, enabled, interval_minutes, limit_per_source, sort, updated_at)
           VALUES (1, 0, 1440, 20, 'latest', ?)""",
        (datetime.now(timezone.utc).isoformat(),),
    )

    # Retire unsupported scrape configurations without rewriting image provenance.
    from app.providers.booru import BOORU_SITES
    from app.providers.gallery_dl import GALLERY_DL_PROVIDERS
    supported = [*BOORU_SITES, *GALLERY_DL_PROVIDERS]
    placeholders = ','.join('?' for _ in supported)
    conn.execute(f'DELETE FROM collection_source WHERE provider NOT IN ({placeholders})', supported)
    conn.execute(f"UPDATE artist_group SET provider='retired' WHERE provider NOT IN ({placeholders})", supported)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
