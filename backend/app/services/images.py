from app.services.diagnostics import emit
import hashlib
import imagehash
import shutil
import re
from PIL import Image
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone
import json
import mimetypes
import sqlite3

from app.db import get_connection
from app.models import RemotePost, Image as ImageModel
from app.services.dedup import DedupService, analyze_training_image
from app.services.media import prepare_training_image
from app.services.tags import TagService
from app.services.storage_lock import pair_write


class ImageService:
    """Service for image ingestion, deduplication, and management."""

    def __init__(self, library_path: Path):
        self.library_path = library_path
        self.images_path = library_path / "images"
        self.thumbnails_path = library_path / "thumbnails"

        # Create directories
        self.images_path.mkdir(parents=True, exist_ok=True)
        self.thumbnails_path.mkdir(parents=True, exist_ok=True)

    def compute_sha256(self, file_path: str) -> str:
        """Compute SHA256 hash of a file."""
        return self.compute_hashes(file_path, include_md5=False)[0]

    def compute_md5(self, file_path: str) -> str:
        """Compute MD5 hash of a file."""
        return self.compute_hashes(file_path, include_md5=True)[1] or ""

    @staticmethod
    def compute_hashes(file_path: str, include_md5: bool = True) -> tuple[str, Optional[str]]:
        """Hash a file in one pass using large sequential reads."""
        sha256_hash = hashlib.sha256()
        md5_hash = hashlib.md5() if include_md5 else None
        with open(file_path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                sha256_hash.update(block)
                if md5_hash is not None:
                    md5_hash.update(block)
        return sha256_hash.hexdigest(), md5_hash.hexdigest() if md5_hash is not None else None

    def compute_phash(self, file_path: str) -> int:
        """Compute perceptual hash of an image."""
        try:
            with Image.open(file_path) as source:
                # Convert to RGB if necessary. Keep all file handles scoped to
                # this block so Windows permits the subsequent move.
                img = source.convert('RGB') if source.mode not in ('RGB', 'L') else source.copy()
            phash = imagehash.phash(img)
            img.close()
            # Convert to integer
            value = int(str(phash), 16)
            # SQLite INTEGER is signed 64-bit; keep the full hash while avoiding
            # overflow, then mask back to 64 bits when comparing hashes.
            return value - (1 << 64) if value >= (1 << 63) else value
        except Exception as e:
            emit("service.notice", f"Error computing phash for {file_path}: {e}", "WARNING")
            return 0

    def generate_thumbnail(self, image_path: str, thumb_path: str, size: Tuple[int, int] = (300, 300)):
        """Generate a thumbnail for an image."""
        try:
            with Image.open(image_path) as source:
                # Work on a detached copy; the source handle must be closed
                # before any later filesystem operations.
                img = source.convert('RGB') if source.mode not in ('RGB', 'L') else source.copy()
            img.thumbnail(size, Image.Resampling.LANCZOS)
            Path(thumb_path).parent.mkdir(parents=True, exist_ok=True)
            img.save(thumb_path, "JPEG", quality=85)
            img.close()
        except Exception as e:
            emit("service.notice", f"Error generating thumbnail for {image_path}: {e}", "WARNING")

    def check_existing_by_hash(
        self,
        sha256: Optional[str] = None,
        md5: Optional[str] = None,
        folder_id: Optional[int] = None,
    ) -> Optional[int]:
        """Check if an image already exists inside one folder."""
        conn = get_connection()
        cursor = conn.cursor()
        folder_clause = " AND folder_id = ?" if folder_id is not None else ""

        if sha256:
            cursor.execute(
                f"SELECT id FROM image WHERE sha256 = ?{folder_clause}",
                (sha256, folder_id) if folder_id is not None else (sha256,),
            )
            result = cursor.fetchone()
            if result:
                conn.close()
                return result[0]

        if md5:
            cursor.execute(
                f"SELECT id FROM image WHERE md5 = ?{folder_clause}",
                (md5, folder_id) if folder_id is not None else (md5,),
            )
            result = cursor.fetchone()
            if result:
                conn.close()
                return result[0]

        conn.close()
        return None

    def check_existing_by_source(
        self,
        provider: str,
        remote_id: str,
        folder_id: int,
    ) -> Optional[int]:
        """Find an already imported provider asset without touching its media."""
        conn = get_connection()
        row = conn.execute(
            """SELECT i.id
               FROM image_source source
               JOIN image i ON i.id = source.image_id
               WHERE source.provider = ? AND source.remote_id = ? AND i.folder_id = ?
               ORDER BY source.version DESC LIMIT 1""",
            (provider, remote_id, folder_id),
        ).fetchone()
        conn.close()
        return int(row[0]) if row else None

    def existing_frame_count(self, provider: str, remote_id: str, folder_id: int) -> int:
        """Return the recorded output count for an already extracted motion source."""
        return self.existing_frames(provider, remote_id, folder_id)[1]

    def existing_frames(self, provider: str, remote_id: str, folder_id: int) -> tuple[list, int]:
        """Read all frame identities and their output count in one snapshot/query."""
        keys = [f"{remote_id}:frame:{number}" for number in range(1, 4)]
        conn = get_connection()
        try:
            rows = conn.execute("""
                SELECT source.remote_id, i.id, source.metadata
                FROM image_source source JOIN image i ON i.id = source.image_id
                WHERE source.provider = ? AND source.remote_id IN (?, ?, ?)
                  AND i.folder_id = ? ORDER BY source.version DESC
            """, (provider, *keys, folder_id)).fetchall()
        finally:
            conn.close()
        latest = {}
        for row in rows:
            latest.setdefault(row[0], row)
        ids = [latest[key][1] if key in latest else None for key in keys]
        count = 0
        if keys[0] in latest:
            try:
                count = int(json.loads(latest[keys[0]][2]).get("_artist_collection_import", {}).get("frame_count", 0))
            except (TypeError, ValueError, AttributeError):
                pass
        return ids, count if 1 <= count <= 3 else 0

    def check_near_duplicate(self, phash: int, threshold: int = 10) -> Optional[int]:
        """Check for near-duplicate images using perceptual hash. Returns image_id if found."""
        conn = get_connection()
        cursor = conn.cursor()

        # Get all phashes
        cursor.execute("SELECT id, phash FROM image WHERE phash IS NOT NULL")
        rows = cursor.fetchall()

        for row in rows:
            existing_id, existing_phash = row[0], row[1]
            if existing_phash is not None:
                # Compute Hamming distance
                distance = ((phash & ((1 << 64) - 1)) ^ (existing_phash & ((1 << 64) - 1))).bit_count()
                if distance <= threshold:
                    conn.close()
                    return existing_id

        conn.close()
        return None

    def ingest_image(
        self,
        temp_path: str,
        post: RemotePost,
        collection_id: int,
        derived_from_preview: bool = False,
        original_media_format: Optional[str] = None,
        original_media_url: Optional[str] = None,
        derived_media_source: Optional[str] = None,
    ) -> Tuple[int, bool]:
        """
        Ingest an image into the library.

        Returns:
            Tuple of (image_id, is_new)
        """
        # All ingest paths share one trainer-file policy before content hashes,
        # dimensions, thumbnails, and duplicate fingerprints are computed.
        from app.services.settings import get_processing_settings
        from app.services.server import require_space
        require_space(self.library_path)
        post = prepare_training_image(temp_path, post, get_processing_settings())

        # A provider MD5 belongs to the original media. A converted video/GIF
        # preview must be keyed by its own downloaded bytes instead.
        is_derived = bool(derived_media_source or derived_from_preview)
        use_provider_md5 = bool(post.md5 and not is_derived)
        sha256, computed_md5 = self.compute_hashes(temp_path, include_md5=not use_provider_md5)
        md5 = post.md5 if use_provider_md5 else computed_md5

        # Check for exact duplicate
        existing_id = self.check_existing_by_hash(sha256, md5, collection_id)
        if existing_id:
            # Link to collection
            self._link_to_collection(existing_id, collection_id)
            # Add source record
            self._add_source_record(existing_id, post)
            TagService(self.library_path)._rewrite_sidecar(existing_id)
            # Ingest consumes its temporary input on both the new and reuse
            # paths. This prevents cross-provider exact matches from leaking
            # downloads in long-running sync jobs.
            Path(temp_path).unlink(missing_ok=True)
            return existing_id, False

        # Keep each collection's images and sidecars together. The canonical
        # file remains content-addressed within that collection folder.
        conn = get_connection()
        collection_row = conn.execute("SELECT slug FROM collection WHERE id = ?", (collection_id,)).fetchone()
        conn.close()
        collection_slug = collection_row[0] if collection_row else f"collection-{collection_id}"

        # Decode the final training file only once for fingerprints, metadata,
        # and its UI thumbnail. This is especially valuable for lossless WebP.
        thumb_relative = f"{collection_slug}/{sha256}_thumb.jpg"
        thumb_path = self.thumbnails_path / thumb_relative
        fingerprint, width, height, format_ext = analyze_training_image(temp_path, thumb_path)
        relative_path = f"{collection_slug}/{sha256}.{format_ext}"
        dest_path = self.images_path / relative_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Move file to library
        # The Windows temp directory and the library may be on different
        # drives; shutil.move falls back to copy-and-remove in that case.
        shutil.move(temp_path, dest_path)

        # Get file size
        file_size = dest_path.stat().st_size

        # Insert into database
        conn = get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                INSERT INTO image (
                    sha256, md5, phash, dhash, gradient_hash, colorhash,
                    width, height, format, file_size, path, thumb_path, added_at,
                    derived_from_preview, original_media_format, original_media_url,
                    derived_media_source, folder_id, processing_policy
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sha256,
                md5,
                fingerprint.phash,
                fingerprint.dhash,
                fingerprint.gradient_hash,
                fingerprint.colorhash,
                width,
                height,
                format_ext,
                file_size,
                relative_path,
                thumb_relative,
                datetime.now(timezone.utc).isoformat(),
                1 if (derived_media_source == "provider_preview" or derived_from_preview) else 0,
                original_media_format if is_derived else None,
                original_media_url if is_derived else None,
                derived_media_source or ("provider_preview" if derived_from_preview else None),
                collection_id,
                json.dumps(post.raw_metadata.get("_artist_folder_processing")),
            ))
            image_id = cursor.lastrowid
            conn.commit()
            conn.close()
        except sqlite3.IntegrityError:
            # Another worker can finish ingesting the same bytes after our
            # initial duplicate check. Reuse its row instead of failing the job.
            conn.rollback()
            conn.close()
            existing_id = self.check_existing_by_hash(sha256=sha256, folder_id=collection_id)
            if not existing_id:
                raise
            self._link_to_collection(existing_id, collection_id)
            self._add_source_record(existing_id, post)
            TagService(self.library_path)._rewrite_sidecar(existing_id)
            return existing_id, False

        # Link to collection
        self._link_to_collection(image_id, collection_id)

        # Add source record
        self._add_source_record(image_id, post)
        TagService(self.library_path)._rewrite_sidecar(image_id)
        try:
            DedupService(self.library_path).record_candidates_for_image(image_id)
        except Exception as exc:
            # The image and provenance are already safely committed. A later
            # library scan can rebuild candidates if this secondary step fails.
            emit("service.notice", f"Warning: could not record duplicate candidates for image {image_id}: {exc}", "WARNING")

        return image_id, True

    def _link_to_collection(self, image_id: int, collection_id: int):
        """Link an image to its owning folder; cross-folder links are invalid."""
        conn = get_connection()
        cursor = conn.cursor()

        try:
            owner = cursor.execute("SELECT folder_id FROM image WHERE id = ?", (image_id,)).fetchone()
            if not owner:
                raise LookupError("Image not found")
            if owner[0] is not None and int(owner[0]) != int(collection_id):
                raise ValueError("Images cannot be shared between folders")
            cursor.execute("UPDATE image SET folder_id = ? WHERE id = ? AND folder_id IS NULL", (collection_id, image_id))
            cursor.execute("""
                INSERT OR IGNORE INTO collection_image (collection_id, image_id, added_at)
                VALUES (?, ?, ?)
            """, (collection_id, image_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()

    def _add_source_record(self, image_id: int, post: RemotePost):
        """Add a source record for an image."""
        conn = get_connection()
        cursor = conn.cursor()
        # Serialize version allocation and primary-source selection across
        # parallel workers. The transaction remains very short.
        cursor.execute("BEGIN IMMEDIATE")

        metadata_json = json.dumps(post.raw_metadata, sort_keys=True)
        # Preserve metadata snapshots. A repeated fetch with identical metadata
        # is idempotent; changed metadata gets the next version.
        cursor.execute("""
            SELECT id, version, metadata, is_primary FROM image_source
            WHERE image_id = ? AND provider = ? AND remote_id = ?
            ORDER BY version DESC LIMIT 1
        """, (image_id, post.provider, post.remote_id))

        latest = cursor.fetchone()
        if latest and latest[2] == metadata_json:
            # Category normalization can improve independently of the raw
            # provider snapshot (for example, Gelbooru's separate tag index).
            # Refresh only the denormalized rows for this immutable version.
            cursor.execute("DELETE FROM image_tag WHERE source_id = ?", (latest[0],))
            self._insert_source_tags(cursor, image_id, latest[0], post)
            conn.commit()
            conn.close()
            return

        version = (int(latest[1]) + 1) if latest else 1
        if latest:
            is_primary = int(latest[3])
        else:
            has_primary = cursor.execute(
                "SELECT 1 FROM image_source WHERE image_id = ? AND is_primary = 1 LIMIT 1",
                (image_id,),
            ).fetchone()
            is_primary = 0 if has_primary else 1

        # Insert source record
        cursor.execute("""
            INSERT INTO image_source (
                image_id, provider, remote_id, remote_url, source_url, fetched_at,
                metadata, version, metadata_version, is_primary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            image_id,
            post.provider,
            post.remote_id,
            post.remote_url,
            post.remote_url,
            datetime.now(timezone.utc).isoformat(),
            metadata_json,
            version,
            version,
            is_primary,
        ))

        source_id = cursor.lastrowid

        self._insert_source_tags(cursor, image_id, source_id, post)

        conn.commit()
        conn.close()

    @staticmethod
    def _insert_source_tags(cursor, image_id: int, source_id: int, post: RemotePost) -> None:
        cursor.executemany("""
            INSERT INTO image_tag (image_id, source_id, category, tag, score)
            VALUES (?, ?, ?, ?, ?)
        """, ((image_id, source_id, category, tag, None)
              for category, tags in post.tags.items() for tag in tags))

    def get_image_by_id(self, image_id: int) -> Optional[dict]:
        """Get image by ID with all metadata."""
        conn = get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM image WHERE id = ?", (image_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return None

        image = dict(row)
        image['sidecar_path'] = str(Path(image['path']).with_suffix('.txt')).replace('\\', '/')

        # Get sources
        cursor.execute("SELECT * FROM image_source WHERE image_id = ?", (image_id,))
        sources = [dict(r) for r in cursor.fetchall()]
        # The original publication date on the site, from each stored metadata snapshot.
        from app.services.post_dates import posted_at
        for source in sources:
            try:
                source['posted_at'] = posted_at(json.loads(source.get('metadata') or '{}'))
            except (TypeError, ValueError):
                source['posted_at'] = None
        dated = [source for source in sources if source['posted_at']]
        primary = next((source for source in dated if source.get('is_primary')), None)
        image['posted_at'] = primary['posted_at'] if primary else min((source['posted_at'] for source in dated), default=None)
        image['posted_on'] = (primary or (min(dated, key=lambda source: source['posted_at']) if dated else {})).get('provider')
        image['sources'] = sources
        from app import db
        from app.services.storage_locations import image_storage_locations
        image['storage_locations'] = image_storage_locations(image, self.library_path, db.DB_PATH)

        # Get tags
        cursor.execute("""
            SELECT category, tag FROM image_tag WHERE image_id = ?
        """, (image_id,))
        tags = {}
        for row in cursor.fetchall():
            category, tag = row[0], row[1]
            if category not in tags:
                tags[category] = []
            tags[category].append(tag)
        image['tags'] = tags
        # Source tags above remain immutable provenance. This flat list is the
        # editable trainer-facing truth after applying add/remove overrides.
        image['ground_truth_tags'] = TagService(self.library_path).get_ground_truth(image_id)['tags']

        # Get collections
        cursor.execute("""
            SELECT c.name FROM collection c
            JOIN collection_image ci ON c.id = ci.collection_id
            WHERE ci.image_id = ?
        """, (image_id,))
        image['folders'] = [r[0] for r in cursor.fetchall()]
        image['collections'] = image['folders']  # legacy API compatibility

        conn.close()
        return image

    def update_review(self, image_id: int, updates: dict) -> Optional[dict]:
        allowed = {"review_status", "favorite", "notes"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if "review_status" in values and values["review_status"] not in {"pending", "accepted", "rejected", "archived"}:
            raise ValueError("review_status must be pending, accepted, rejected, or archived")
        if not values:
            return self.get_image_by_id(image_id)
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [int(value) if isinstance(value, bool) else value for value in values.values()]
        params.append(image_id)
        conn = get_connection()
        conn.execute(f"UPDATE image SET {assignments} WHERE id = ?", params)
        changed = conn.total_changes
        conn.commit()
        conn.close()
        return self.get_image_by_id(image_id) if changed else None

    @pair_write
    def delete_image(self, image_id: int) -> bool:
        conn = get_connection()
        row = conn.execute("SELECT path, thumb_path FROM image WHERE id = ?", (image_id,)).fetchone()
        if not row:
            conn.close()
            return False
        conn.execute("DELETE FROM image WHERE id = ?", (image_id,))
        conn.commit()
        conn.close()
        from app.services.captions import caption_relative, get_suffix
        caption = caption_relative(row[0], get_suffix()) if row[0] else None
        for relative in (row[0], row[1], str(Path(row[0]).with_suffix('.txt')) if row[0] else None, caption):
            if relative:
                root = self.thumbnails_path if relative == row[1] else self.images_path
                (root / relative).unlink(missing_ok=True)
        return True

    def migrate_to_collection_folders(self) -> int:
        """Move legacy hash-sharded files into their collection folders."""
        conn = get_connection()
        rows = conn.execute("""
            SELECT i.id, i.path, i.thumb_path, COALESCE((
                SELECT c.slug FROM collection c JOIN collection_image ci ON ci.collection_id = c.id
                WHERE ci.image_id = i.id ORDER BY ci.added_at LIMIT 1
            ), 'unassigned') AS slug
            FROM image i
            WHERE i.path IS NOT NULL
        """).fetchall()
        moved = 0
        for row in rows:
            old_path = self.images_path / row[1]
            path_parts = str(row[1]).replace("\\", "/").split("/")
            if len(path_parts) < 2 or not re.fullmatch(r"[0-9a-fA-F]{3}", path_parts[0]) or not old_path.exists():
                continue
            filename = Path(row[1]).name
            new_rel = f"{row[3]}/{filename}"
            new_path = self.images_path / new_rel
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if not new_path.exists():
                shutil.move(str(old_path), str(new_path))
            from app.services.captions import caption_file, get_suffix
            old_caption, new_caption = caption_file(old_path, get_suffix()), caption_file(new_path, get_suffix())
            if old_caption.exists() and not new_caption.exists():
                shutil.move(str(old_caption), str(new_caption))
            old_sidecar = old_path.with_suffix('.txt')
            new_sidecar = new_path.with_suffix('.txt')
            if old_sidecar.exists() and not new_sidecar.exists():
                shutil.move(str(old_sidecar), str(new_sidecar))
            if new_sidecar.exists():
                # Trainer captions use spaces, never booru underscores.
                new_sidecar.write_text(new_sidecar.read_text(encoding="utf-8").replace("_", " "), encoding="utf-8")
            thumb_rel = row[2]
            new_thumb_rel = None
            if thumb_rel:
                old_thumb = self.thumbnails_path / thumb_rel
                new_thumb_rel = f"{row[3]}/{Path(thumb_rel).name}"
                new_thumb = self.thumbnails_path / new_thumb_rel
                new_thumb.parent.mkdir(parents=True, exist_ok=True)
                if old_thumb.exists() and not new_thumb.exists():
                    shutil.move(str(old_thumb), str(new_thumb))
            owner = conn.execute("SELECT id FROM collection WHERE slug = ?", (row[3],)).fetchone()
            conn.execute(
                "UPDATE image SET path = ?, thumb_path = ?, folder_id = COALESCE(folder_id, ?) WHERE id = ?",
                (new_rel, new_thumb_rel, owner[0] if owner else None, row[0]),
            )
            moved += 1
        conn.commit()
        conn.close()
        return moved

    @pair_write
    def reconcile_filesystem(self) -> dict:
        """Rebuild missing index rows from collection folders without deleting files."""
        from app.services.groups import recover_moves
        recover_moves(self.library_path)
        valid_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif"}
        now = datetime.now(timezone.utc).isoformat()
        stats = {"collections": 0, "images": 0, "thumbnails": 0, "tags": 0, "errors": 0}
        conn = get_connection()
        groups = {row['slug']: dict(row) for row in conn.execute('SELECT * FROM artist_group')}
        folders = []
        for parent in self.images_path.iterdir() if self.images_path.exists() else []:
            if parent.name in groups and parent.is_dir():
                folders.extend((child, groups[parent.name]) for child in parent.iterdir() if child.is_dir())
            else:
                folders.append((parent, None))
        for folder, group in folders:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            slug = folder.relative_to(self.images_path).as_posix()
            if self.images_path.resolve() not in folder.resolve().parents or folder.is_symlink():
                continue
            collection = conn.execute("SELECT id FROM collection WHERE slug = ?", (slug,)).fetchone()
            if collection:
                collection_id = collection[0]
            else:
                display_name = folder.name.replace("-", " ")
                # Avoid colliding with another folder's display name during recovery.
                if conn.execute('SELECT 1 FROM collection WHERE group_id IS ? AND name=?', (group['id'] if group else None, display_name)).fetchone():
                    display_name = folder.name
                conn.execute("""
                    INSERT INTO collection (name, slug, type, query, caption_template, filters, created_at, updated_at, enabled, group_id)
                    VALUES (?, ?, 'artist', ?, ?, ?, ?, ?, 1, ?)
                """, (
                    display_name, slug, display_name,
                    json.dumps({"version": 1, "trigger": "", "include_categories": ["artist", "character", "copyright", "species", "general", "meta"], "separator": ", "}),
                    json.dumps({"allowed_formats": sorted(ext.lstrip('.') for ext in valid_extensions)}), now, now, group['id'] if group else None,
                ))
                collection_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute('UPDATE collection SET group_id=? WHERE id=?', (group['id'] if group else None, collection_id))
                conn.execute("INSERT OR IGNORE INTO collection_source (collection_id, provider, enabled) VALUES (?, ?, 1)", (collection_id, group['provider'] if group else 'danbooru'))
                stats["collections"] += 1

            for image_path in folder.iterdir():
                if not image_path.is_file() or image_path.suffix.lower() not in valid_extensions:
                    continue
                relative = image_path.relative_to(self.images_path).as_posix()
                sha256, md5 = self.compute_hashes(str(image_path), include_md5=True)
                existing = conn.execute(
                    "SELECT id FROM image WHERE folder_id = ? AND sha256 = ?",
                    (collection_id, sha256),
                ).fetchone()
                if existing:
                    image_id = existing[0]
                else:
                    thumb_rel = f"{slug}/{image_path.stem}_thumb.jpg"
                    thumb_path = self.thumbnails_path / thumb_rel
                    needs_thumbnail = not thumb_path.exists()
                    try:
                        fingerprint, width, height, format_ext = analyze_training_image(
                            image_path,
                            thumb_path if needs_thumbnail else None,
                        )
                    except Exception as exc:
                        stats["errors"] += 1
                        emit("service.notice", f"Skipping unreadable image during reconciliation: {image_path}: {exc}", "WARNING")
                        continue
                    if needs_thumbnail:
                        stats["thumbnails"] += 1
                    conn.execute("""
                        INSERT INTO image (
                            sha256, md5, phash, dhash, gradient_hash, colorhash,
                            width, height, format, file_size, path, thumb_path, added_at, folder_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        sha256, md5, fingerprint.phash, fingerprint.dhash,
                        fingerprint.gradient_hash, fingerprint.colorhash, width, height,
                        format_ext, image_path.stat().st_size, relative, thumb_rel, now, collection_id,
                    ))
                    image_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    stats["images"] += 1

                conn.execute("INSERT OR IGNORE INTO collection_image (collection_id, image_id, added_at) VALUES (?, ?, ?)", (collection_id, image_id, now))
                sidecar = image_path.with_suffix(".txt")
                if sidecar.exists() and not conn.execute("SELECT 1 FROM image_source WHERE image_id = ? LIMIT 1", (image_id,)).fetchone():
                    raw_tags = [tag.strip() for tag in sidecar.read_text(encoding="utf-8", errors="replace").replace("_", " ").split(",") if tag.strip()]
                    metadata = json.dumps({"recovered_from": "sidecar", "tags": raw_tags}, sort_keys=True)
                    conn.execute("INSERT INTO image_source (image_id, provider, remote_id, remote_url, fetched_at, metadata, version) VALUES (?, 'filesystem', ?, NULL, ?, ?, 1)", (image_id, sha256, now, metadata))
                    source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    for tag in raw_tags:
                        conn.execute("INSERT INTO image_tag (image_id, source_id, category, tag, score) VALUES (?, ?, 'general', ?, NULL)", (image_id, source_id, tag))
                    stats["tags"] += len(raw_tags)
        conn.commit()
        conn.close()
        return stats
