"""Perceptual duplicate detection and human-reviewed merge operations."""

from __future__ import annotations
from app.services.diagnostics import emit

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import imagehash
from PIL import Image, ImageOps

from app.db import get_connection
from app.services.tags import TagService
from app.services.storage_lock import pair_write


MASK64 = (1 << 64) - 1
ProgressCallback = Callable[[dict], None]


PROFILES = {
    "strict": {
        "phash": 5,
        "dhash": 6,
        "gradient": 8,
        "minimum_score": 90.0,
        "minimum_aspect": 0.80,
    },
    "balanced": {
        "phash": 10,
        "dhash": 12,
        "gradient": 20,
        "minimum_score": 84.0,
        "minimum_aspect": 0.68,
    },
    "broad": {
        "phash": 16,
        "dhash": 18,
        "gradient": 32,
        "minimum_score": 75.0,
        "minimum_aspect": 0.55,
    },
}


def to_signed_64(value: int) -> int:
    value &= MASK64
    return value - (1 << 64) if value >= (1 << 63) else value


def to_unsigned_64(value: int) -> int:
    return int(value) & MASK64


def hamming_distance(left: int, right: int, bits: int = 64) -> int:
    mask = (1 << bits) - 1
    return ((int(left) & mask) ^ (int(right) & mask)).bit_count()


def hex_hamming_distance(left: str, right: str) -> int:
    if not left or not right or len(left) != len(right):
        raise ValueError("Hashes must be non-empty and have the same length")
    return hamming_distance(int(left, 16), int(right, 16), len(left) * 4)


def color_distance(left: str, right: str) -> int:
    """Return a normalized 0..100 distance between RGB layout signatures."""
    try:
        left_bytes = bytes.fromhex(left)
        right_bytes = bytes.fromhex(right)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid color signature") from exc
    if not left_bytes or len(left_bytes) != len(right_bytes):
        raise ValueError("Color signatures must have the same length")
    difference = sum(abs(a - b) for a, b in zip(left_bytes, right_bytes))
    return round((difference / (len(left_bytes) * 255)) * 100)


@dataclass(frozen=True)
class Fingerprint:
    phash: int
    dhash: int
    gradient_hash: str
    colorhash: str


def compute_fingerprint(file_path: Path | str) -> Fingerprint:
    """Compute format-independent structural and color fingerprints."""
    fingerprint, _, _, _ = analyze_training_image(file_path)
    return fingerprint


def analyze_training_image(
    file_path: Path | str,
    thumbnail_path: Path | str | None = None,
    thumbnail_size: tuple[int, int] = (300, 300),
) -> tuple[Fingerprint, int, int, str]:
    """Decode once for fingerprints, dimensions, format, and thumbnail."""
    with Image.open(file_path) as source:
        format_ext = (source.format or Path(file_path).suffix.lstrip(".")).lower()
        oriented = ImageOps.exif_transpose(source)
        rgb = oriented.convert("RGB")
        width, height = rgb.size

    try:
        phash = to_signed_64(int(str(imagehash.phash(rgb, hash_size=8)), 16))
        dhash = to_signed_64(int(str(imagehash.dhash(rgb, hash_size=8)), 16))
        # Czkawka's current CLI defaults to a 16x16 Gradient hash. imagehash's
        # dHash is the same horizontal-gradient family and gives us a stricter
        # 256-bit structural signal alongside the compact 64-bit hashes.
        gradient_hash = str(imagehash.dhash(rgb, hash_size=16))
        color_grid = rgb.resize((4, 4), Image.Resampling.LANCZOS)
        colorhash = color_grid.tobytes().hex()
        color_grid.close()
        if thumbnail_path is not None:
            thumbnail = rgb.copy()
            try:
                thumbnail.thumbnail(thumbnail_size, Image.Resampling.LANCZOS)
                destination = Path(thumbnail_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    thumbnail.save(destination, "JPEG", quality=85)
                except Exception as exc:
                    # A UI thumbnail must never make an otherwise valid
                    # training image fail ingestion.
                    emit("service.notice", f"Error generating thumbnail for {file_path}: {exc}", "WARNING")
            finally:
                thumbnail.close()
    finally:
        rgb.close()

    return Fingerprint(phash, dhash, gradient_hash, colorhash), width, height, format_ext


class _BKTree:
    """Small in-memory BK-tree for Hamming-radius candidate lookup."""

    def __init__(self, distance: Callable[[int, int], int]):
        self.distance = distance
        self.root: Optional[list] = None

    def add(self, value: int, image_id: int) -> None:
        if self.root is None:
            self.root = [value, [image_id], {}]
            return
        node = self.root
        while True:
            node_value, ids, children = node
            distance = self.distance(value, node_value)
            if distance == 0:
                ids.append(image_id)
                return
            if distance not in children:
                children[distance] = [value, [image_id], {}]
                return
            node = children[distance]

    def find(self, value: int, radius: int) -> set[int]:
        if self.root is None:
            return set()
        matches: set[int] = set()
        pending = [self.root]
        while pending:
            node_value, ids, children = pending.pop()
            distance = self.distance(value, node_value)
            if distance <= radius:
                matches.update(ids)
            lower, upper = distance - radius, distance + radius
            pending.extend(child for edge, child in children.items() if lower <= edge <= upper)
        return matches


class DedupService:
    """Find visual candidates, expose review data, and merge only on request."""

    def __init__(self, library_path: Path):
        self.library_path = Path(library_path)
        self.images_path = self.library_path / "images"
        self.thumbnails_path = self.library_path / "thumbnails"

    def scan(
        self,
        collection_id: Optional[int] = None,
        profile: str = "balanced",
        progress: Optional[ProgressCallback] = None,
    ) -> dict:
        if profile not in PROFILES:
            raise ValueError(f"Unknown scan profile: {profile}")
        settings = PROFILES[profile]
        conn = get_connection()
        if collection_id is not None and not conn.execute(
            "SELECT 1 FROM collection WHERE id = ?", (collection_id,)
        ).fetchone():
            conn.close()
            raise ValueError("Folder not found")

        target_ids: Optional[set[int]] = None
        if collection_id is not None:
            target_ids = {row[0] for row in conn.execute(
                "SELECT image_id FROM collection_image WHERE collection_id = ?", (collection_id,)
            ).fetchall()}
        # Folder-owned datasets are independent. A folder scan never compares
        # or merges files owned by another folder.
        if collection_id is None:
            rows = [dict(row) for row in conn.execute("SELECT * FROM image ORDER BY id").fetchall()]
        else:
            rows = [dict(row) for row in conn.execute(
                """SELECT i.* FROM image i
                   JOIN collection_image ci ON ci.image_id = i.id
                   WHERE ci.collection_id = ? ORDER BY i.id""",
                (collection_id,),
            ).fetchall()]
        fingerprinted = 0
        missing = 0
        failures: list[str] = []
        valid_image_ids: set[int] = set()

        for index, row in enumerate(rows, 1):
            needs_fingerprint = any(row.get(field) is None for field in ("phash", "dhash", "gradient_hash", "colorhash"))
            path = self.images_path / row["path"]
            if not path.exists():
                missing += 1
                failures.append(f"Missing image file for image {row['id']}")
            elif needs_fingerprint:
                try:
                    fingerprint = compute_fingerprint(path)
                    conn.execute(
                        "UPDATE image SET phash = ?, dhash = ?, gradient_hash = ?, colorhash = ? WHERE id = ?",
                        (fingerprint.phash, fingerprint.dhash, fingerprint.gradient_hash, fingerprint.colorhash, row["id"]),
                    )
                    row.update(
                        phash=fingerprint.phash,
                        dhash=fingerprint.dhash,
                        gradient_hash=fingerprint.gradient_hash,
                        colorhash=fingerprint.colorhash,
                    )
                    fingerprinted += 1
                except Exception as exc:
                    failures.append(f"Could not fingerprint image {row['id']}: {exc}")
            if path.exists() and all(row.get(field) is not None for field in ("phash", "dhash", "gradient_hash", "colorhash")):
                valid_image_ids.add(int(row["id"]))
            if needs_fingerprint and fingerprinted and fingerprinted % 25 == 0:
                # Avoid holding one writer transaction throughout a large
                # library backfill while normal imports are still running.
                conn.commit()
            if progress:
                progress({
                    "phase": "fingerprints",
                    "completed": index,
                    "total": max(len(rows), 1),
                    "message": f"Fingerprinting images: {index}/{len(rows)}",
                })
        conn.commit()

        usable = [row for row in rows if int(row["id"]) in valid_image_ids]
        phash_tree = _BKTree(lambda a, b: hamming_distance(a, b, 64))
        dhash_tree = _BKTree(lambda a, b: hamming_distance(a, b, 64))
        gradient_tree = _BKTree(lambda a, b: hamming_distance(a, b, 256))
        by_id = {int(row["id"]): row for row in usable}
        for row in usable:
            phash_tree.add(to_unsigned_64(row["phash"]), row["id"])
            dhash_tree.add(to_unsigned_64(row["dhash"]), row["id"])
            gradient_tree.add(int(row["gradient_hash"], 16), row["id"])

        candidates_found = 0
        candidates_created = 0
        rows_to_query = usable if target_ids is None else [row for row in usable if row["id"] in target_ids]
        seen_pairs: set[tuple[int, int]] = set()
        for index, row in enumerate(rows_to_query, 1):
            image_id = int(row["id"])
            possible = phash_tree.find(to_unsigned_64(row["phash"]), settings["phash"])
            possible.update(dhash_tree.find(to_unsigned_64(row["dhash"]), settings["dhash"]))
            possible.update(gradient_tree.find(int(row["gradient_hash"], 16), settings["gradient"]))
            for other_id in sorted(
                candidate for candidate in possible
                if candidate != image_id and by_id[candidate].get("folder_id") == row.get("folder_id")
            ):
                image_a, image_b = sorted((image_id, other_id))
                if (image_a, image_b) in seen_pairs:
                    continue
                seen_pairs.add((image_a, image_b))
                metrics = self._compare(row, by_id[other_id])
                if metrics["aspect_similarity"] < settings["minimum_aspect"]:
                    continue
                if metrics["visual_similarity"] < settings["minimum_score"]:
                    continue
                matched_methods = []
                if metrics["phash_distance"] <= settings["phash"]:
                    matched_methods.append("pHash")
                if metrics["dhash_distance"] <= settings["dhash"]:
                    matched_methods.append("dHash")
                if metrics["gradient_distance"] <= settings["gradient"]:
                    matched_methods.append("Czkawka-style gradient")
                matched_methods.append("XnView-style color and layout")
                before = conn.total_changes
                conn.execute(
                    """
                    INSERT INTO duplicate_candidate (
                        image_id_a, image_id_b, phash_distance, dhash_distance,
                        color_distance, visual_similarity, methods, status, created_at,
                        gradient_distance, aspect_similarity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    ON CONFLICT(image_id_a, image_id_b) DO UPDATE SET
                        phash_distance = excluded.phash_distance,
                        dhash_distance = excluded.dhash_distance,
                        color_distance = excluded.color_distance,
                        visual_similarity = excluded.visual_similarity,
                        methods = excluded.methods,
                        gradient_distance = excluded.gradient_distance,
                        aspect_similarity = excluded.aspect_similarity
                    """,
                    (
                        image_a,
                        image_b,
                        metrics["phash_distance"],
                        metrics["dhash_distance"],
                        metrics["color_distance"],
                        metrics["visual_similarity"],
                        json.dumps(matched_methods),
                        datetime.now(timezone.utc).isoformat(),
                        metrics["gradient_distance"],
                        metrics["aspect_similarity"],
                    ),
                )
                candidates_created += int(conn.total_changes > before)
                candidates_found += 1
                if candidates_found % 250 == 0:
                    conn.commit()
            if progress:
                progress({
                    "phase": "comparison",
                    "completed": index,
                    "total": max(len(rows_to_query), 1),
                    "candidates": candidates_found,
                    "message": f"Comparing images: {index}/{len(rows_to_query)}",
                })
        conn.commit()
        pending = conn.execute("SELECT COUNT(*) FROM duplicate_candidate WHERE status = 'pending'").fetchone()[0]
        conn.close()
        return {
            "profile": profile,
            "images_scanned": len(rows),
            "fingerprints_added": fingerprinted,
            "missing_files": missing,
            "candidates_found": candidates_found,
            "candidates_written": candidates_created,
            "pending_candidates": pending,
            "warnings": failures,
        }

    def record_candidates_for_image(self, image_id: int, profile: str = "balanced") -> int:
        """Record candidates for a newly ingested, already-fingerprinted image."""
        settings = PROFILES[profile]
        conn = get_connection()
        current_row = conn.execute("SELECT * FROM image WHERE id = ?", (image_id,)).fetchone()
        if not current_row:
            conn.close()
            return 0
        current = dict(current_row)
        required = ("phash", "dhash", "gradient_hash", "colorhash")
        if any(current.get(field) is None for field in required):
            conn.close()
            return 0
        aspect = current["width"] / max(current["height"], 1)
        minimum_aspect = settings["minimum_aspect"]
        others = [dict(row) for row in conn.execute(
            """SELECT id, width, height, phash, dhash, gradient_hash, colorhash
               FROM image WHERE id <> ? AND folder_id IS ?
               AND phash IS NOT NULL AND dhash IS NOT NULL
               AND gradient_hash IS NOT NULL AND colorhash IS NOT NULL
               AND height > 0
               AND (CAST(width AS REAL) / height) BETWEEN ? AND ?""",
            (
                image_id,
                current.get("folder_id"),
                aspect * minimum_aspect,
                aspect / minimum_aspect,
            ),
        ).fetchall()]
        created = 0
        for other in others:
            metrics = self._compare(current, other)
            close = (
                metrics["phash_distance"] <= settings["phash"]
                or metrics["dhash_distance"] <= settings["dhash"]
                or metrics["gradient_distance"] <= settings["gradient"]
            )
            if not close or metrics["visual_similarity"] < settings["minimum_score"] or metrics["aspect_similarity"] < settings["minimum_aspect"]:
                continue
            image_a, image_b = sorted((image_id, int(other["id"])))
            methods = [
                name for name, matches in (
                    ("pHash", metrics["phash_distance"] <= settings["phash"]),
                    ("dHash", metrics["dhash_distance"] <= settings["dhash"]),
                    ("Czkawka-style gradient", metrics["gradient_distance"] <= settings["gradient"]),
                ) if matches
            ] + ["XnView-style color and layout"]
            conn.execute(
                """
                INSERT OR IGNORE INTO duplicate_candidate (
                    image_id_a, image_id_b, phash_distance, dhash_distance, color_distance,
                    visual_similarity, methods, status, created_at, gradient_distance, aspect_similarity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (image_a, image_b, metrics["phash_distance"], metrics["dhash_distance"], metrics["color_distance"],
                 metrics["visual_similarity"], json.dumps(methods), datetime.now(timezone.utc).isoformat(),
                 metrics["gradient_distance"], metrics["aspect_similarity"]),
            )
            created += conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
        conn.close()
        return created

    def _compare(self, left: dict, right: dict) -> dict:
        phash = hamming_distance(left["phash"], right["phash"], 64)
        dhash = hamming_distance(left["dhash"], right["dhash"], 64)
        gradient = hex_hamming_distance(left["gradient_hash"], right["gradient_hash"])
        color = color_distance(left["colorhash"], right["colorhash"])
        left_aspect = left["width"] / max(left["height"], 1)
        right_aspect = right["width"] / max(right["height"], 1)
        aspect = min(left_aspect, right_aspect) / max(left_aspect, right_aspect)
        similarity = 100 * (
            0.24 * (1 - phash / 64)
            + 0.18 * (1 - dhash / 64)
            + 0.23 * (1 - gradient / 256)
            + 0.25 * (1 - color / 100)
            + 0.10 * aspect
        )
        return {
            "phash_distance": phash,
            "dhash_distance": dhash,
            "gradient_distance": gradient,
            "color_distance": color,
            "aspect_similarity": round(aspect, 4),
            "visual_similarity": round(max(0.0, min(100.0, similarity)), 2),
        }

    def list_candidates(
        self,
        status: str = "pending",
        collection_id: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        allowed_statuses = {"pending", "kept_both", "not_duplicate", "merged"}
        if status not in allowed_statuses:
            raise ValueError("Invalid duplicate status")
        where = ["dc.status = ?"]
        params: list = [status]
        if collection_id is not None:
            where.append("""(
                EXISTS (SELECT 1 FROM collection_image cia WHERE cia.image_id = dc.image_id_a AND cia.collection_id = ?)
                OR EXISTS (SELECT 1 FROM collection_image cib WHERE cib.image_id = dc.image_id_b AND cib.collection_id = ?)
            )""")
            params.extend((collection_id, collection_id))
        where_sql = " AND ".join(where)
        conn = get_connection()
        total = conn.execute(f"SELECT COUNT(*) FROM duplicate_candidate dc WHERE {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT dc.*, a.sha256 AS a_sha256, a.width AS a_width, a.height AS a_height,
                   a.format AS a_format, a.file_size AS a_file_size, a.path AS a_path,
                   a.thumb_path AS a_thumb_path, a.added_at AS a_added_at,
                   b.sha256 AS b_sha256, b.width AS b_width, b.height AS b_height,
                   b.format AS b_format, b.file_size AS b_file_size, b.path AS b_path,
                   b.thumb_path AS b_thumb_path, b.added_at AS b_added_at
            FROM duplicate_candidate dc
            JOIN image a ON a.id = dc.image_id_a
            JOIN image b ON b.id = dc.image_id_b
            WHERE {where_sql}
            ORDER BY dc.visual_similarity DESC, dc.id
            LIMIT ? OFFSET ?
            """,
            [*params, min(max(limit, 1), 500), max(offset, 0)],
        ).fetchall()
        items = []
        for raw in rows:
            row = dict(raw)
            item = {key: row[key] for key in (
                "id", "image_id_a", "image_id_b", "phash_distance", "dhash_distance",
                "gradient_distance", "color_distance", "aspect_similarity", "visual_similarity",
                "status", "created_at", "resolved_at", "resolved_action", "kept_image_id",
            )}
            try:
                item["methods"] = json.loads(row["methods"])
            except (TypeError, json.JSONDecodeError):
                item["methods"] = [row["methods"]]
            item["image_a"] = self._candidate_image(row, "a")
            item["image_b"] = self._candidate_image(row, "b")
            items.append(item)
        conn.close()
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _candidate_image(row: dict, prefix: str) -> dict:
        return {
            "id": row[f"image_id_{prefix}"],
            "sha256": row[f"{prefix}_sha256"],
            "width": row[f"{prefix}_width"],
            "height": row[f"{prefix}_height"],
            "format": row[f"{prefix}_format"],
            "file_size": row[f"{prefix}_file_size"],
            "path": row[f"{prefix}_path"],
            "thumb_path": row[f"{prefix}_thumb_path"],
            "added_at": row[f"{prefix}_added_at"],
        }

    @pair_write
    def resolve(self, candidate_id: int, action: str) -> dict:
        allowed = {"keep_a", "keep_b", "keep_highest_quality", "keep_both", "not_duplicate"}
        if action not in allowed:
            raise ValueError("Invalid duplicate action")
        conn = get_connection()
        candidate = conn.execute("SELECT * FROM duplicate_candidate WHERE id = ?", (candidate_id,)).fetchone()
        if not candidate:
            conn.close()
            raise LookupError("Duplicate candidate not found")
        if candidate["status"] != "pending":
            conn.close()
            raise ValueError("This duplicate candidate has already been resolved")
        now = datetime.now(timezone.utc).isoformat()
        if action in {"keep_both", "not_duplicate"}:
            status = "kept_both" if action == "keep_both" else "not_duplicate"
            conn.execute(
                "UPDATE duplicate_candidate SET status = ?, resolved_action = ?, resolved_at = ? WHERE id = ?",
                (status, action, now, candidate_id),
            )
            conn.commit()
            conn.close()
            return {"candidate_id": candidate_id, "status": status, "action": action}

        image_a = conn.execute("SELECT * FROM image WHERE id = ?", (candidate["image_id_a"],)).fetchone()
        image_b = conn.execute("SELECT * FROM image WHERE id = ?", (candidate["image_id_b"],)).fetchone()
        if not image_a or not image_b:
            conn.close()
            raise LookupError("One of the candidate images no longer exists")
        if image_a["folder_id"] != image_b["folder_id"]:
            conn.close()
            raise ValueError("Images from different folders cannot be merged")
        if action == "keep_a":
            winner, loser = image_a, image_b
        elif action == "keep_b":
            winner, loser = image_b, image_a
        else:
            winner, loser = max((image_a, image_b), key=self._quality_key), min((image_a, image_b), key=self._quality_key)

        tag_service = TagService(self.library_path)
        winner_effective_tags = tag_service._effective_tags(conn, int(winner["id"]))
        loser_effective_tags = tag_service._effective_tags(conn, int(loser["id"]))
        merged_effective_tags = list(dict.fromkeys(
            [*winner_effective_tags, *loser_effective_tags]
        ))
        audit_details = {
            "candidate": dict(candidate),
            "winner": dict(winner),
            "loser": dict(loser),
            "loser_collections": [dict(row) for row in conn.execute(
                "SELECT * FROM collection_image WHERE image_id = ?", (loser["id"],)
            ).fetchall()],
            "loser_sources": [dict(row) for row in conn.execute(
                "SELECT * FROM image_source WHERE image_id = ?", (loser["id"],)
            ).fetchall()],
            "winner_tag_overrides": tag_service._override_rows(conn, int(winner["id"])),
            "loser_tag_overrides": tag_service._override_rows(conn, int(loser["id"])),
            "merged_effective_tags": merged_effective_tags,
        }
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT OR IGNORE INTO collection_image
                    (collection_id, image_id, added_at, review_status, selected, rejection_reason)
                SELECT collection_id, ?, added_at, review_status, selected, rejection_reason
                FROM collection_image WHERE image_id = ?
                """,
                (winner["id"], loser["id"]),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO collection_caption
                    (collection_id, image_id, caption, generated_at, template_version)
                SELECT collection_id, ?, caption, generated_at, template_version
                FROM collection_caption WHERE image_id = ?
                """,
                (winner["id"], loser["id"]),
            )
            conn.execute("UPDATE image_tag SET image_id = ? WHERE image_id = ?", (winner["id"], loser["id"]))
            conn.execute("UPDATE image_source SET image_id = ? WHERE image_id = ?", (winner["id"], loser["id"]))
            self._replace_effective_tags_after_merge(
                conn, int(winner["id"]), int(loser["id"]), merged_effective_tags, now
            )
            self._normalize_primary_source(conn, int(winner["id"]))
            conn.execute(
                """
                INSERT INTO duplicate_resolution
                    (candidate_id, action, kept_image_id, removed_image_id, kept_sha256,
                     removed_sha256, details, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (candidate_id, action, winner["id"], loser["id"], winner["sha256"], loser["sha256"],
                 json.dumps(audit_details, sort_keys=True), now),
            )
            conn.execute("DELETE FROM image WHERE id = ?", (loser["id"],))
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()

        self._delete_image_files(dict(loser))
        self._rewrite_sidecar(int(winner["id"]), Path(winner["path"]))
        return {
            "candidate_id": candidate_id,
            "status": "merged",
            "action": action,
            "kept_image_id": winner["id"],
            "removed_image_id": loser["id"],
        }

    @staticmethod
    def _replace_effective_tags_after_merge(
        conn, winner_id: int, loser_id: int, desired_tags: list[str], now: str
    ) -> None:
        source_tags = TagService._source_tags(conn, winner_id)
        source_by_key = {tag.casefold(): tag for tag in source_tags}
        desired_by_key = {tag.casefold(): tag for tag in desired_tags}
        conn.execute(
            "DELETE FROM image_tag_override WHERE image_id IN (?, ?)",
            (winner_id, loser_id),
        )
        desired_positions = {tag.casefold(): index for index, tag in enumerate(desired_tags)}
        for key, tag in desired_by_key.items():
            if key not in source_by_key:
                conn.execute(
                    """INSERT INTO image_tag_override
                       (image_id, tag, action, category, updated_at, position)
                       VALUES (?, ?, 'add', 'general', ?, ?)""",
                    (winner_id, tag, now, desired_positions[key]),
                )
        for key, tag in source_by_key.items():
            if key not in desired_by_key:
                conn.execute(
                    """INSERT INTO image_tag_override
                       (image_id, tag, action, category, updated_at)
                       VALUES (?, ?, 'remove', 'general', ?)""",
                    (winner_id, tag, now),
                )

    @staticmethod
    def _normalize_primary_source(conn, image_id: int) -> None:
        primary = conn.execute(
            """SELECT provider, remote_id FROM image_source
               WHERE image_id = ? AND provider <> 'filesystem'
               ORDER BY id LIMIT 1""",
            (image_id,),
        ).fetchone()
        conn.execute("UPDATE image_source SET is_primary = 0 WHERE image_id = ?", (image_id,))
        if primary:
            conn.execute(
                """UPDATE image_source SET is_primary = 1
                   WHERE image_id = ? AND provider = ? AND remote_id = ?""",
                (image_id, primary["provider"], primary["remote_id"]),
            )

    @staticmethod
    def _quality_key(image) -> tuple:
        format_priority = {"png": 5, "webp": 4, "tiff": 4, "tif": 4, "jpeg": 3, "jpg": 3, "bmp": 2}
        return (
            int(image["width"]) * int(image["height"]),
            format_priority.get(str(image["format"]).lower(), 1),
            int(image["file_size"]),
            -int(image["id"]),
        )

    def _delete_image_files(self, image: dict) -> None:
        targets = [
            (self.images_path, image.get("path")),
            (self.thumbnails_path, image.get("thumb_path")),
        ]
        if image.get("path"):
            targets.append((self.images_path, str(Path(image["path"]).with_suffix(".txt"))))
        for root, relative in targets:
            if not relative:
                continue
            safe_root = root.resolve()
            target = (safe_root / relative).resolve()
            if safe_root in target.parents:
                target.unlink(missing_ok=True)
                parent = target.parent
                if parent != safe_root and parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()

    def _rewrite_sidecar(self, image_id: int, relative_path: Path) -> None:
        conn = get_connection()
        tags = TagService(self.library_path)._effective_tags(conn, image_id)
        conn.close()
        sidecar = (self.images_path / relative_path).with_suffix(".txt")
        if sidecar.exists() or tags:
            sidecar.write_text(", ".join(tags), encoding="utf-8")
