from app.services.diagnostics import emit
import json
import shutil
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from pathlib import Path

from app.db import get_connection
from app.models import CollectionCreate, Collection, CollectionWithStats
from app.services.queries import humanize_artist_query
from app.services.storage_lock import pair_write


class CollectionService:
    """Service for managing collections."""

    def create_collection(self, collection_data: CollectionCreate, *, connection=None) -> Collection:
        """Create a new collection."""
        conn = connection or get_connection()
        cursor = conn.cursor()
        try:
            from app.services.groups import folder_slug, validate_group
            if connection is None:
                conn.execute('BEGIN IMMEDIATE')
            group = validate_group(conn, collection_data.group_id)
            slug = folder_slug(collection_data.name)
            if group:
                slug = f"{group['slug']}/{slug}"
            # Ensure uniqueness
            base_slug = slug
            counter = 1
            while True:
                cursor.execute("SELECT id FROM collection WHERE lower(slug) = lower(?) UNION ALL SELECT id FROM artist_group WHERE lower(slug) = lower(?)", (slug, slug))
                if not cursor.fetchone():
                    break
                slug = f"{base_slug}-{counter}"
                counter += 1

            now = datetime.now(timezone.utc).isoformat()
            stored_query = (
                humanize_artist_query(collection_data.query)
                if collection_data.type == "artist"
                else collection_data.query.strip()
            )
            cursor.execute("""
                INSERT INTO collection (
                    name, slug, type, query, artist_tag_template,
                    caption_template, filters, created_at, updated_at, enabled, group_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                collection_data.name,
                slug,
                collection_data.type,
                stored_query,
                collection_data.artist_tag_template,
                json.dumps(collection_data.caption_template),
                json.dumps(collection_data.filters),
                now,
                now,
                1,
                collection_data.group_id
            ))

            collection_id = cursor.lastrowid
            for provider in collection_data.sources:
                cursor.execute("""
                    INSERT INTO collection_source (collection_id, provider, enabled, query_override)
                    VALUES (?, ?, ?, ?)
                """, (collection_id, provider, 1, collection_data.source_queries.get(provider) or None))

            if connection is None:
                conn.commit()
            cursor.execute("SELECT * FROM collection WHERE id = ?", (collection_id,))
            row = cursor.fetchone()
            return self._row_to_collection(dict(row))
        except Exception:
            if connection is None:
                conn.rollback()
            raise
        finally:
            if connection is None:
                conn.close()

    def get_collection(self, collection_id: int) -> Optional[CollectionWithStats]:
        """Get a collection by ID with stats."""
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM collection WHERE id = ?", (collection_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return None

        collection = dict(row)

        # Get image count
        cursor.execute("""
            SELECT COUNT(*) FROM collection_image WHERE collection_id = ?
        """, (collection_id,))
        collection['image_count'] = cursor.fetchone()[0]

        # Get sources
        cursor.execute("""
            SELECT provider, last_cursor, backfill_cursor, enabled, query_override FROM collection_source
            WHERE collection_id = ?
        """, (collection_id,))
        collection['sources'] = [
            {'provider': r[0], 'last_cursor': r[1], 'backfill_cursor': r[2], 'enabled': bool(r[3]), 'query_override': r[4]}
            for r in cursor.fetchall()
        ]

        conn.close()
        return self._row_to_collection_with_stats(collection)

    def list_collections(self) -> List[CollectionWithStats]:
        """List all collections with stats."""
        conn = get_connection()
        cursor = conn.cursor()

        rows = cursor.execute("SELECT * FROM collection ORDER BY created_at DESC").fetchall()
        counts = dict(cursor.execute('SELECT collection_id,COUNT(*) FROM collection_image GROUP BY collection_id').fetchall())
        sources = {}
        for row in cursor.execute('SELECT collection_id,provider,last_cursor,backfill_cursor,enabled,query_override FROM collection_source'):
            sources.setdefault(row[0], []).append({
                'provider': row[1], 'last_cursor': row[2], 'backfill_cursor': row[3],
                'enabled': bool(row[4]), 'query_override': row[5],
            })
        collections = []
        for row in rows:
            collection = dict(row)
            collection['image_count'] = counts.get(row['id'], 0)
            collection['sources'] = sources.get(row['id'], [])
            collections.append(self._row_to_collection_with_stats(collection))

        conn.close()
        return collections

    def update_collection(
        self,
        collection_id: int,
        updates: Dict[str, Any]
    ) -> Optional[Collection]:
        """Update a collection."""
        conn = get_connection()
        cursor = conn.cursor()
        existing = cursor.execute("SELECT type FROM collection WHERE id = ?", (collection_id,)).fetchone()
        if 'type' in updates and existing and updates['type'] != existing[0]:
            conn.close()
            raise ValueError('Collection type is fixed to protect existing tags. Create a new collection for another search type.')
        if 'query' in updates:
            from app.services.queries import validate_collection_query, assert_search_idle
            try:
                assert_search_idle(conn, collection_id)
                validate_collection_query(updates['query'], existing[0] if existing else 'artist')
            except ValueError:
                conn.close()
                raise
        if existing and existing[0] != 'artist' and updates.get('artist_tag_template'):
            conn.close()
            raise ValueError('Artist folder-name triggers are available only for artist collections')
        target_type = updates.get('type', existing[0] if existing else None)

        # Build update query
        allowed_fields = ['name', 'type', 'query', 'artist_tag_template', 'caption_template', 'filters', 'enabled']
        set_clauses = []
        values = []

        for field, value in updates.items():
            if field in allowed_fields:
                if field == 'query':
                    value = humanize_artist_query(value) if target_type == 'artist' else value.strip()
                if field in ['caption_template', 'filters']:
                    value = json.dumps(value)
                set_clauses.append(f"{field} = ?")
                values.append(value)

        if not set_clauses:
            conn.close()
            return None

        # Add updated_at
        set_clauses.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(collection_id)

        query = f"UPDATE collection SET {', '.join(set_clauses)} WHERE id = ?"
        cursor.execute(query, values)
        conn.commit()

        if "query" in updates:
            cursor.execute("UPDATE collection_source SET last_cursor=NULL, backfill_cursor=NULL WHERE collection_id=?", (collection_id,))
            conn.commit()

        # Fetch updated collection
        cursor.execute("SELECT * FROM collection WHERE id = ?", (collection_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return self._row_to_collection(dict(row))
        return None

    @pair_write
    def delete_collection(self, collection_id: int, library_path: Optional[Path] = None) -> bool:
        """Delete a folder and its isolated image, sidecar, and thumbnail namespace."""
        conn = get_connection()
        cursor = conn.cursor()

        collection = cursor.execute("SELECT slug FROM collection WHERE id = ?", (collection_id,)).fetchone()
        if not collection:
            conn.close()
            return False
        if library_path:
            # A folder deletion is permanent, including any batch-delete
            # recovery snapshot that still belongs to it.
            self._purge_image_recoveries(collection_id, library_path)
        cursor.execute("DELETE FROM collection WHERE id = ?", (collection_id,))
        conn.commit()
        conn.close()

        if library_path:
            images_root = (library_path / "images").resolve()
            thumbs_root = (library_path / "thumbnails").resolve()
            # A folder owns its complete storage namespace. Purging the exact
            # slug directory also removes files whose review membership was
            # previously removed, plus unindexed leftovers from interrupted
            # syncs. The resolved-parent guard prevents traversal/broad deletes.
            for root in (images_root, thumbs_root):
                collection_dir = (root / collection[0]).resolve()
                if root not in collection_dir.parents:
                    raise ValueError(f"Unsafe folder storage path: {collection_dir}")
                if collection_dir.exists():
                    shutil.rmtree(collection_dir)
        return True

    def get_collection_images(
        self,
        collection_id: int,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Get images in a collection."""
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT i.* FROM image i
            JOIN collection_image ci ON i.id = ci.image_id
            WHERE ci.collection_id = ?
            ORDER BY ci.added_at DESC
            LIMIT ? OFFSET ?
        """, (collection_id, limit, offset))

        images = []
        for row in cursor.fetchall():
            image = dict(row)
            image['sidecar_path'] = str(Path(image['path']).with_suffix('.txt')).replace('\\', '/')
            images.append(image)
        conn.close()

        return images

    @staticmethod
    def _safe_library_path(root: Path, relative: str) -> Path:
        root = root.resolve()
        candidate = (root / relative).resolve()
        if candidate == root or root not in candidate.parents:
            raise ValueError(f"Unsafe library path: {relative}")
        return candidate

    @pair_write
    def remove_images(
        self,
        collection_id: int,
        image_ids: List[int],
        library_path: Path,
        undo_token: str,
    ) -> List[Dict[str, Any]]:
        """Move complete folder-owned pairs to recovery storage and deactivate them."""
        if not image_ids:
            return []
        if not undo_token or any(character not in "0123456789abcdef" for character in undo_token.lower()):
            raise ValueError("Invalid recovery token")
        conn = get_connection()
        placeholders = ",".join("?" for _ in image_ids)
        rows = conn.execute(f"""
            SELECT ? AS collection_id, i.id AS image_id,
                   COALESCE(ci.added_at, i.added_at) AS added_at,
                   COALESCE(ci.review_status, 'pending') AS review_status,
                   COALESCE(ci.selected, 1) AS selected,
                   ci.rejection_reason, i.path, i.thumb_path
            FROM image i
            LEFT JOIN collection_image ci
              ON ci.collection_id = ? AND ci.image_id = i.id
            WHERE i.folder_id = ? AND i.id IN ({placeholders})
        """, [collection_id, collection_id, collection_id, *image_ids]).fetchall()
        records = [dict(row) for row in rows]
        if not records:
            conn.close()
            return []

        images_root = (library_path / "images").resolve()
        thumbs_root = (library_path / "thumbnails").resolve()
        recovery_root = (library_path / ".trash" / undo_token).resolve()
        expected_recovery_parent = (library_path / ".trash").resolve()
        if expected_recovery_parent not in recovery_root.parents:
            conn.close()
            raise ValueError("Unsafe recovery path")

        move_plan: list[tuple[Path, Path]] = []
        for record in records:
            image_path = self._safe_library_path(images_root, record["path"])
            move_plan.append((image_path, self._safe_library_path(recovery_root / "images", record["path"])))
            sidecar_path = image_path.with_suffix(".txt")
            move_plan.append((sidecar_path, self._safe_library_path(recovery_root / "images", str(Path(record["path"]).with_suffix(".txt")))))
            if record.get("thumb_path"):
                thumb_path = self._safe_library_path(thumbs_root, record["thumb_path"])
                move_plan.append((thumb_path, self._safe_library_path(recovery_root / "thumbnails", record["thumb_path"])))

        moved: list[tuple[Path, Path]] = []
        manifest_path = recovery_root / "manifest.json"
        try:
            for source, destination in move_plan:
                if not source.exists():
                    continue
                if destination.exists():
                    raise FileExistsError(f"Recovery target already exists: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                moved.append((source, destination))
            recovery_root.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps({
                "token": undo_token,
                "collection_id": collection_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "images": records,
            }, indent=2), encoding="utf-8")
            conn.execute(
                f"DELETE FROM collection_image WHERE collection_id = ? AND image_id IN ({placeholders})",
                [collection_id, *image_ids],
            )
            conn.execute(
                f"UPDATE image SET folder_id = NULL WHERE folder_id = ? AND id IN ({placeholders})",
                [collection_id, *image_ids],
            )
            remaining_image = conn.execute(
                "SELECT 1 FROM image WHERE folder_id = ? LIMIT 1", (collection_id,)
            ).fetchone()
            if not remaining_image:
                # A fully emptied folder must start a new scan at the head;
                # its old high-water and older-page offsets refer to files
                # that are no longer part of the active collection.
                conn.execute(
                    "UPDATE collection_source SET last_cursor = NULL, backfill_cursor = NULL WHERE collection_id = ?",
                    (collection_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination), str(source))
            manifest_path.unlink(missing_ok=True)
            raise
        finally:
            conn.close()
        # "Recover last deletion" is intentionally singular. Once a new
        # batch succeeds, discard older snapshots so large training sets do
        # not silently accumulate duplicate data in recovery storage.
        try:
            self._purge_image_recoveries(collection_id, library_path, keep_token=undo_token)
        except Exception as exc:
            emit("service.notice", f"Warning: could not purge an older batch-delete recovery: {exc}", "WARNING")
        return records

    @pair_write
    def restore_images(self, collection_id: int, undo_token: str, library_path: Path) -> int:
        if not undo_token or any(character not in "0123456789abcdef" for character in undo_token.lower()):
            raise ValueError("Invalid recovery token")
        recovery_root = (library_path / ".trash" / undo_token).resolve()
        trash_root = (library_path / ".trash").resolve()
        if trash_root not in recovery_root.parents:
            raise ValueError("Unsafe recovery path")
        manifest_path = recovery_root / "manifest.json"
        if not manifest_path.exists():
            raise LookupError("Undo action expired or not found")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(manifest.get("collection_id", -1)) != int(collection_id):
            raise ValueError("Undo action belongs to another folder")
        records = manifest.get("images") or []
        if not records:
            return 0

        images_root = (library_path / "images").resolve()
        thumbs_root = (library_path / "thumbnails").resolve()
        conn = get_connection()
        restored_moves: list[tuple[Path, Path]] = []
        try:
            folder = conn.execute("SELECT slug FROM collection WHERE id = ?", (collection_id,)).fetchone()
            if not folder:
                raise LookupError("Folder no longer exists")
            for record in records:
                image = conn.execute(
                    "SELECT sha256, folder_id FROM image WHERE id = ?", (record["image_id"],)
                ).fetchone()
                if not image:
                    raise LookupError(f"Recoverable image {record['image_id']} no longer exists")
                if image[1] is not None and int(image[1]) != int(collection_id):
                    raise ValueError(f"Image {record['image_id']} is now owned by another folder")
                conflict = conn.execute(
                    "SELECT 1 FROM image WHERE folder_id = ? AND sha256 = ? AND id <> ?",
                    (collection_id, image[0], record["image_id"]),
                ).fetchone()
                if conflict:
                    raise ValueError("Cannot recover because the same image was imported again")

            restore_plan: list[tuple[Path, Path]] = []
            for record in records:
                # Trash retains its original namespace; recovery follows a moved folder.
                record['restore_path'] = f"{folder[0]}/{Path(record['path']).name}"
                record['restore_thumb_path'] = f"{folder[0]}/{Path(record['thumb_path']).name}" if record.get('thumb_path') else None
                restore_plan.append((
                    self._safe_library_path(recovery_root / "images", record["path"]),
                    self._safe_library_path(images_root, record["restore_path"]),
                ))
                restore_plan.append((
                    self._safe_library_path(recovery_root / "images", str(Path(record["path"]).with_suffix(".txt"))),
                    self._safe_library_path(images_root, str(Path(record["restore_path"]).with_suffix(".txt"))),
                ))
                if record.get("thumb_path"):
                    restore_plan.append((
                        self._safe_library_path(recovery_root / "thumbnails", record["thumb_path"]),
                        self._safe_library_path(thumbs_root, record["restore_thumb_path"]),
                    ))
            for source, destination in restore_plan:
                if not source.exists():
                    continue
                if destination.exists():
                    raise FileExistsError(f"Active target already exists: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                restored_moves.append((source, destination))

            for record in records:
                conn.execute("UPDATE image SET folder_id = ?, path = ?, thumb_path = ? WHERE id = ?",
                             (collection_id, record['restore_path'], record['restore_thumb_path'], record["image_id"]))
                conn.execute("""
                    INSERT INTO collection_image
                    (collection_id, image_id, added_at, review_status, selected, rejection_reason)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    collection_id, record["image_id"], record["added_at"],
                    record.get("review_status", "pending"), record.get("selected", 1), record.get("rejection_reason"),
                ))
            conn.commit()
        except Exception:
            conn.rollback()
            for source, destination in reversed(restored_moves):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination), str(source))
            raise
        finally:
            conn.close()

        manifest_path.unlink(missing_ok=True)
        for directory in sorted((path for path in recovery_root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            recovery_root.rmdir()
        except OSError:
            pass
        return len(records)

    def latest_image_recovery(self, collection_id: int, library_path: Path) -> Optional[Dict[str, Any]]:
        """Return the newest durable batch-delete manifest for a folder."""
        trash_root = (library_path / ".trash").resolve()
        if not trash_root.exists():
            return None
        recoveries = []
        for recovery_root in trash_root.iterdir():
            if not recovery_root.is_dir() or any(character not in "0123456789abcdef" for character in recovery_root.name.lower()):
                continue
            manifest_path = recovery_root / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if int(manifest.get("collection_id", -1)) != int(collection_id):
                    continue
                recoveries.append({
                    "token": recovery_root.name,
                    "created_at": manifest.get("created_at"),
                    "image_count": len(manifest.get("images") or []),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return max(recoveries, key=lambda item: item.get("created_at") or "") if recoveries else None

    def _purge_image_recoveries(
        self,
        collection_id: int,
        library_path: Path,
        keep_token: Optional[str] = None,
    ) -> int:
        """Permanently discard superseded recovery snapshots for one folder."""
        trash_root = (library_path / ".trash").resolve()
        if not trash_root.exists():
            return 0
        purged = 0
        for recovery_root in list(trash_root.iterdir()):
            token = recovery_root.name
            if token == keep_token or not recovery_root.is_dir() or any(character not in "0123456789abcdef" for character in token.lower()):
                continue
            manifest_path = recovery_root / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if int(manifest.get("collection_id", -1)) != int(collection_id):
                continue
            image_ids = [int(record["image_id"]) for record in manifest.get("images") or []]
            conn = get_connection()
            try:
                if image_ids:
                    placeholders = ",".join("?" for _ in image_ids)
                    conn.execute(
                        f"DELETE FROM image WHERE folder_id IS NULL AND id IN ({placeholders})",
                        image_ids,
                    )
                conn.commit()
            finally:
                conn.close()
            resolved = recovery_root.resolve()
            if trash_root not in resolved.parents:
                raise ValueError(f"Unsafe recovery purge path: {resolved}")
            shutil.rmtree(resolved)
            purged += 1
        return purged

    def _row_to_collection(self, row: Dict[str, Any]) -> Collection:
        """Convert database row to Collection model."""
        return Collection(
            id=row['id'],
            name=row['name'],
            slug=row['slug'],
            group_id=row.get('group_id'),
            type=row['type'],
            query=row['query'],
            artist_tag_template=row.get('artist_tag_template'),
            caption_template=json.loads(row['caption_template']),
            filters=json.loads(row['filters']),
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            last_sync_at=row.get('last_sync_at'),
            enabled=bool(row['enabled'])
        )

    def _row_to_collection_with_stats(self, row: Dict[str, Any]) -> CollectionWithStats:
        """Convert database row to CollectionWithStats model."""
        return CollectionWithStats(
            id=row['id'],
            name=row['name'],
            slug=row['slug'],
            group_id=row.get('group_id'),
            type=row['type'],
            query=row['query'],
            artist_tag_template=row.get('artist_tag_template'),
            caption_template=json.loads(row['caption_template']) if isinstance(row['caption_template'], str) else row['caption_template'],
            filters=json.loads(row['filters']) if isinstance(row['filters'], str) else row['filters'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            last_sync_at=row.get('last_sync_at'),
            enabled=bool(row['enabled']),
            image_count=row.get('image_count', 0),
            sources=row.get('sources', [])
        )
