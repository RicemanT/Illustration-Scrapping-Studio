"""Read-only trainer-dataset validation and immutable exports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from app.db import get_connection
from app.services.media import MAX_TRAINING_DIMENSION, MIN_TRAINING_DIMENSION
from app.services.tags import TagService
from app.services.storage_lock import pair_write


SUPPORTED_STILL_FORMATS = {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "avif"}
TRAINING_FORMATS = {"jpg", "jpeg", "webp"}
BLOCKING_CODES = {
    "missing_file",
    "missing_sidecar",
    "corrupt_image",
    "unsupported_format",
    "empty_ground_truth_tags",
    "path_collision",
    "sidecar_collision",
    "hash_mismatch",
    "unsafe_path",
    "unsafe_sidecar_path",
    "sidecar_mismatch",
    "wrong_folder_owner",
    "oversized_image",
    "undersized_image",
    "disallowed_aspect_ratio",
    "noncanonical_training_format",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"unparsed": value}


class DatasetQAService:
    def __init__(self, library_path: Path):
        self.library_path = Path(library_path)
        self.images_path = (self.library_path / "images").resolve()
        self.exports_path = self.library_path / "exports"
        self.tag_service = TagService(self.library_path)

    def validate_collection(self, collection_id: int, image_ids: list[int] | None = None) -> dict:
        conn = get_connection()
        try:
            return self._validate_collection(conn, collection_id, image_ids)
        finally:
            conn.close()

    def _validate_collection(self, conn, collection_id: int, image_ids: list[int] | None = None) -> dict:
        collection = conn.execute(
            "SELECT id, name, slug, filters FROM collection WHERE id = ?", (collection_id,)
        ).fetchone()
        if not collection:
            conn.close()
            raise LookupError("Folder not found")
        rows = conn.execute(
            """SELECT i.* FROM image i
               JOIN collection_image ci ON ci.image_id = i.id
               WHERE ci.collection_id = ? AND (? IS NULL OR i.id IN (SELECT value FROM json_each(?))) ORDER BY i.id""",
            (collection_id, json.dumps(image_ids) if image_ids is not None else None, json.dumps(image_ids)),
        ).fetchall()
        try:
            folder_filters = json.loads(collection["filters"] or "{}")
        except (TypeError, json.JSONDecodeError):
            folder_filters = {}
        min_width = max(MIN_TRAINING_DIMENSION, int(folder_filters.get("min_width", MIN_TRAINING_DIMENSION) or MIN_TRAINING_DIMENSION))
        min_height = max(MIN_TRAINING_DIMENSION, int(folder_filters.get("min_height", MIN_TRAINING_DIMENSION) or MIN_TRAINING_DIMENSION))
        min_ratio = max(0.0, float(folder_filters.get("min_aspect_ratio", 0) or 0))
        max_ratio = max(0.0, float(folder_filters.get("max_aspect_ratio", 0) or 0))

        issues: list[dict] = []
        path_owners: dict[str, list[int]] = {}
        sidecar_owners: dict[str, list[int]] = {}
        valid_ids: list[int] = []
        if not rows and image_ids is None:
            issues.append(self._issue(None, "empty_folder", "error", "Folder has no images to export", ""))
        for row in rows:
            image_id = int(row["id"])
            relative = str(row["path"] or "")
            normalized = relative.replace("\\", "/").casefold()
            sidecar_relative = str(Path(relative).with_suffix(".txt")).replace("\\", "/")
            path_owners.setdefault(normalized, []).append(image_id)
            sidecar_owners.setdefault(sidecar_relative.casefold(), []).append(image_id)
            image_issues: list[dict] = []
            if row["folder_id"] != collection_id:
                image_issues.append(self._issue(image_id, "wrong_folder_owner", "error", "Image does not own an independent pair in this folder", relative))

            image_path = (self.images_path / relative).resolve()
            folder_root = (self.images_path / collection['slug']).resolve()
            safe_path = self.images_path in folder_root.parents and folder_root in image_path.parents
            if not safe_path:
                image_issues.append(self._issue(image_id, "unsafe_path", "error", "Canonical path escapes the image library", relative))
            elif not image_path.is_file():
                image_issues.append(self._issue(image_id, "missing_file", "error", "Canonical image file is missing", relative))
            else:
                try:
                    policy = json.loads(row["processing_policy"] or "null")
                except (TypeError, ValueError):
                    policy = None
                for source in ([] if policy else conn.execute('SELECT metadata FROM image_source WHERE image_id=? ORDER BY id', (image_id,))):
                    try:
                        candidate = json.loads(source[0] or '{}').get('_artist_folder_processing')
                        if isinstance(candidate, dict):
                            policy = candidate; break
                    except (TypeError, ValueError): pass
                modern_policy = policy and policy.get('version', 1) >= 2
                recorded_limit = policy.get('max_dimension') if modern_policy and policy.get('enabled') else None if modern_policy else MAX_TRAINING_DIMENSION
                file_format = str(row["format"] or "").lower().lstrip(".")
                if file_format not in SUPPORTED_STILL_FORMATS:
                    image_issues.append(self._issue(image_id, "unsupported_format", "error", f"Unsupported canonical still format: {file_format or 'unknown'}", relative))
                elif not modern_policy and file_format not in TRAINING_FORMATS:
                    image_issues.append(self._issue(image_id, "noncanonical_training_format", "error", "Trainer image must be JPEG or lossless WebP; re-import this legacy file", relative))
                if recorded_limit and max(int(row["width"]), int(row["height"])) > recorded_limit:
                    image_issues.append(self._issue(image_id, "oversized_image", "error", f"Longest side exceeds the {recorded_limit}px recorded processing limit", relative))
                width, height = int(row["width"]), int(row["height"])
                quality_width = int(policy.get("source_width", width)) if modern_policy else width
                quality_height = int(policy.get("source_height", height)) if modern_policy else height
                if quality_width < min_width or quality_height < min_height:
                    image_issues.append(self._issue(image_id, "undersized_image", "error", f"Image is {width}x{height}; folder minimum is {min_width}x{min_height}", relative))
                ratio = width / height if height else 0
                if (min_ratio and ratio < min_ratio) or (max_ratio and ratio > max_ratio):
                    image_issues.append(self._issue(image_id, "disallowed_aspect_ratio", "error", f"Image aspect ratio {ratio:.3f} is outside the folder range {min_ratio:g}–{max_ratio:g}", relative))
                try:
                    with Image.open(image_path) as source:
                        source.verify()
                    with Image.open(image_path) as source:
                        source.load()
                except (OSError, ValueError, UnidentifiedImageError) as exc:
                    image_issues.append(self._issue(image_id, "corrupt_image", "error", f"Image cannot be decoded: {exc}", relative))
                try:
                    actual_hash = _sha256(image_path)
                    if actual_hash.casefold() != str(row["sha256"]).casefold():
                        image_issues.append(self._issue(image_id, "hash_mismatch", "error", "File bytes no longer match the indexed SHA-256", relative))
                except OSError as exc:
                    image_issues.append(self._issue(image_id, "corrupt_image", "error", f"Image cannot be read: {exc}", relative))

            tags = self.tag_service._effective_tags(conn, image_id)
            categories = self.tag_service._included_categories(conn, image_id)
            expected_tags = self.tag_service._source_tags(conn, image_id, categories)
            trusted_source = conn.execute("SELECT 1 FROM image_source WHERE image_id=? AND provider IN ('danbooru','gelbooru','e621') LIMIT 1", (image_id,)).fetchone()
            if not tags and (expected_tags or (trusted_source and any(c != 'artist' for c in categories))):
                image_issues.append(self._issue(image_id, "empty_ground_truth_tags", "error", "Ground-truth tag list is empty", sidecar_relative))

            thumb_root = (self.library_path / "thumbnails").resolve()
            thumb_path = (thumb_root / (row["thumb_path"] or "")).resolve()
            if row["thumb_path"] and thumb_root not in thumb_path.parents:
                image_issues.append(self._issue(image_id, "unsafe_thumbnail_path", "warning", "Thumbnail path escapes the thumbnail library", str(thumb_path)))
            elif not row["thumb_path"] or not thumb_path.is_file():
                image_issues.append(self._issue(image_id, "missing_thumbnail", "warning", "UI thumbnail is missing", str(thumb_path)))
            else:
                try:
                    with Image.open(thumb_path) as thumb:
                        thumb.load()
                except (OSError, ValueError) as exc:
                    image_issues.append(self._issue(image_id, "broken_thumbnail", "warning", f"UI thumbnail cannot be decoded: {exc}", str(thumb_path)))
            if not conn.execute("SELECT 1 FROM image_source WHERE image_id=? AND provider<>'filesystem' LIMIT 1", (image_id,)).fetchone():
                image_issues.append(self._issue(image_id, "missing_provenance", "warning", "No imported source provenance is recorded", str(image_path)))
            if conn.execute("""SELECT 1 FROM duplicate_candidate d JOIN image a ON a.id=d.image_id_a
                JOIN image b ON b.id=d.image_id_b WHERE d.status='pending'
                AND a.folder_id=? AND b.folder_id=? AND (a.id=? OR b.id=?) LIMIT 1""",
                (collection_id, collection_id, image_id, image_id)).fetchone():
                image_issues.append(self._issue(image_id, "unresolved_duplicate", "warning", "Possible duplicate requires human review; no automatic merge or deletion", str(image_path)))

            sidecar_path = image_path.with_suffix(".txt")
            resolved_sidecar = sidecar_path.resolve()
            if not safe_path:
                pass
            elif self.images_path != resolved_sidecar and self.images_path not in resolved_sidecar.parents:
                image_issues.append(self._issue(image_id, "unsafe_sidecar_path", "error", "Sidecar path escapes the image library", sidecar_relative))
            elif not sidecar_path.is_file():
                image_issues.append(self._issue(image_id, "missing_sidecar", "error", "Curated UTF-8 sidecar is missing", sidecar_relative))
            else:
                try:
                    actual_caption = sidecar_path.read_text(encoding="utf-8").strip()
                    expected_caption = ", ".join(tags)
                    if actual_caption != expected_caption:
                        image_issues.append(self._issue(image_id, "sidecar_mismatch", "error", "Sidecar does not match the effective curated tags", sidecar_relative))
                except (OSError, UnicodeError) as exc:
                    image_issues.append(self._issue(image_id, "corrupt_sidecar", "error", f"Sidecar is not readable UTF-8: {exc}", sidecar_relative))

            if row["derived_media_source"] == "provider_preview":
                image_issues.append(self._issue(image_id, "unsupported_original_fallback", "error", "Legacy provider-preview still is below the original-quality training policy; delete it and re-import the source", relative))

            # A subset scan still detects collisions with unselected images.
            if image_ids is not None:
                for other in conn.execute("SELECT id,path FROM image WHERE folder_id=? AND id<>? AND path_stem(path)=path_stem(?)", (collection_id, image_id, relative)):
                    other_path = str(other['path']).replace('\\', '/').casefold()
                    for code, matches in (("path_collision", other_path == normalized), ("sidecar_collision", str(Path(other_path).with_suffix('.txt')) == sidecar_relative.casefold())):
                        if matches:
                            image_issues.append(self._issue(image_id, code, "error", "Another active image uses this file/sidecar path", relative))
            # Recheck ownership after expensive I/O; a concurrent deletion is
            # skipped instead of publishing stale missing-file errors.
            if not conn.execute("SELECT 1 FROM image WHERE id=? AND folder_id=?", (image_id, collection_id)).fetchone():
                continue
            for issue in image_issues:
                if not Path(issue['path']).is_absolute():
                    issue['path'] = str((self.images_path / issue['path']).resolve())
                if not safe_path or any(item['code'] in {'wrong_folder_owner', 'path_collision', 'sidecar_collision', 'unsafe_sidecar_path'} for item in image_issues):
                    issue['allowed_actions'] = []
            issues.extend(image_issues)
            if not any(issue["severity"] == "error" for issue in image_issues):
                valid_ids.append(image_id)

        for code, owners in (("path_collision", path_owners), ("sidecar_collision", sidecar_owners)):
            for relative, image_ids in owners.items():
                if relative and len(image_ids) > 1:
                    for image_id in image_ids:
                        issues.append(self._issue(image_id, code, "error", f"Multiple image records resolve to the same {'sidecar' if code == 'sidecar_collision' else 'file'} path", relative, {"image_ids": image_ids}))
                    if any(image_id in valid_ids for image_id in image_ids):
                        valid_ids = [image_id for image_id in valid_ids if image_id not in image_ids]

        conn.close()
        by_code = Counter(issue["code"] for issue in issues)
        errors = sum(issue["severity"] == "error" for issue in issues)
        warnings = sum(issue["severity"] == "warning" for issue in issues)
        return {
            "folder": dict(collection),
            "checked_at": _now(),
            "image_count": len(rows),
            "valid_image_count": len(valid_ids),
            "ready": errors == 0,
            "summary": {"errors": errors, "warnings": warnings, "by_code": dict(sorted(by_code.items()))},
            "issues": issues,
        }

    def _issue(self, image_id: int | None, code: str, severity: str, message: str, path: str, details: dict | None = None) -> dict:
        actions = {"missing_thumbnail": ["thumbnail"], "broken_thumbnail": ["thumbnail"],
                   "missing_sidecar": ["sidecar"], "sidecar_mismatch": ["sidecar"], "corrupt_sidecar": ["sidecar"]}
        backend_path = str((self.images_path / path).resolve()) if path and not Path(path).is_absolute() else path
        return {"image_id": image_id, "code": code, "severity": severity, "message": message, "path": backend_path, "details": details or {}, "allowed_actions": actions.get(code, [])}

    @pair_write
    def create_export(self, collection_id: int, mode: str) -> dict:
        if mode not in {"copy", "hardlink"}:
            raise ValueError("Export mode must be copy or hardlink")
        validation = self.validate_collection(collection_id)
        if not validation["ready"]:
            codes = sorted({issue["code"] for issue in validation["issues"] if issue["code"] in BLOCKING_CODES or issue["severity"] == "error"})
            raise ValueError(f"Folder is not export-ready; resolve validation errors first: {', '.join(codes)}")

        export_id = uuid.uuid4().hex
        collection = validation["folder"]
        output_path = self.exports_path / collection["slug"] / export_id
        created_at = _now()
        conn = get_connection()
        conn.execute(
            """INSERT INTO dataset_export
               (id, collection_id, status, mode, output_path, created_at, warning_count, result)
               VALUES (?, ?, 'running', ?, ?, ?, ?, '{}')""",
            (export_id, collection_id, mode, str(output_path), created_at, validation["summary"]["warnings"]),
        )
        conn.commit()
        conn.close()

        try:
            output_path.mkdir(parents=True, exist_ok=False)
            manifest = self._export_files(export_id, collection_id, collection, output_path, mode, validation)
            manifest_path = output_path / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
            completed_at = _now()
            result = {
                "id": export_id,
                "folder_id": collection_id,
                "status": "completed",
                "mode": mode,
                "output_path": str(output_path),
                "manifest_path": str(manifest_path),
                "created_at": created_at,
                "completed_at": completed_at,
                "image_count": len(manifest["items"]),
                "warning_count": len(manifest["warnings"]),
                "warnings": manifest["warnings"],
            }
            self._finish_export(result)
            return result
        except Exception as exc:
            completed_at = _now()
            result = {
                "id": export_id,
                "folder_id": collection_id,
                "status": "failed",
                "mode": mode,
                "output_path": str(output_path),
                "manifest_path": None,
                "created_at": created_at,
                "completed_at": completed_at,
                "image_count": 0,
                "warning_count": validation["summary"]["warnings"],
                "error": str(exc),
            }
            self._finish_export(result)
            raise

    def _export_files(self, export_id: str, collection_id: int, collection: dict, output_path: Path, mode: str, validation: dict) -> dict:
        conn = get_connection()
        rows = conn.execute(
            """SELECT i.*, ci.review_status AS collection_review_status, ci.selected,
                      ci.rejection_reason
               FROM image i JOIN collection_image ci ON ci.image_id = i.id
               WHERE ci.collection_id = ? ORDER BY i.id""",
            (collection_id,),
        ).fetchall()
        warnings = [issue for issue in validation["issues"] if issue["severity"] == "warning"]
        items: list[dict] = []
        for row in rows:
            source_path = (self.images_path / row["path"]).resolve()
            extension = source_path.suffix.lower() or f".{row['format']}"
            filename = f"{row['sha256']}{extension}"
            sidecar_filename = f"{row['sha256']}.txt"
            effective_mode = mode
            try:
                if mode == "hardlink":
                    os.link(source_path, output_path / filename)
                    os.link(source_path.with_suffix(".txt"), output_path / sidecar_filename)
                else:
                    shutil.copy2(source_path, output_path / filename)
                    shutil.copy2(source_path.with_suffix(".txt"), output_path / sidecar_filename)
            except OSError as exc:
                if mode != "hardlink":
                    raise
                for destination in (output_path / filename, output_path / sidecar_filename):
                    destination.unlink(missing_ok=True)
                shutil.copy2(source_path, output_path / filename)
                shutil.copy2(source_path.with_suffix(".txt"), output_path / sidecar_filename)
                effective_mode = "copy"
                warnings.append(self._issue(row["id"], "hardlink_fallback", "warning", f"Hardlink unavailable; copied this pair instead: {exc}", row["path"]))

            sources = []
            for source in conn.execute("SELECT * FROM image_source WHERE image_id = ? ORDER BY id", (row["id"],)).fetchall():
                item = dict(source)
                item["metadata"] = _safe_json(item["metadata"])
                item["is_primary"] = bool(item.get("is_primary"))
                sources.append(item)
            items.append({
                "image_id": row["id"],
                "filename": filename,
                "sidecar_filename": sidecar_filename,
                "transfer_mode": effective_mode,
                "sha256": row["sha256"],
                "md5": row["md5"],
                "width": row["width"],
                "height": row["height"],
                "format": row["format"],
                "file_size": row["file_size"],
                "ground_truth_tags": self.tag_service._effective_tags(conn, row["id"]),
                "review": {
                    "image_status": row["review_status"],
                    "folder_status": row["collection_review_status"],
                    "selected": bool(row["selected"]),
                    "favorite": bool(row["favorite"]),
                    "notes": row["notes"],
                    "rejection_reason": row["rejection_reason"],
                },
                "derived_media": {
                    "source": row["derived_media_source"],
                    "original_format": row["original_media_format"],
                    "original_url": row["original_media_url"],
                },
                "sources": sources,
            })
        conn.close()
        return {
            "schema_version": 1,
            "export_id": export_id,
            "created_at": _now(),
            "folder": collection,
            "requested_transfer_mode": mode,
            "caption_policy": "existing_curated_sidecars_only",
            "validation": validation["summary"],
            "warnings": warnings,
            "items": items,
        }

    @staticmethod
    def _finish_export(result: dict) -> None:
        conn = get_connection()
        conn.execute(
            """UPDATE dataset_export SET status = ?, manifest_path = ?, completed_at = ?,
                      image_count = ?, warning_count = ?, error = ?, result = ? WHERE id = ?""",
            (result["status"], result.get("manifest_path"), result.get("completed_at"), result.get("image_count", 0), result.get("warning_count", 0), result.get("error"), json.dumps(result, sort_keys=True), result["id"]),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_export(export_id: str) -> dict | None:
        conn = get_connection()
        row = conn.execute("SELECT * FROM dataset_export WHERE id = ?", (export_id,)).fetchone()
        conn.close()
        if not row:
            return None
        result = _safe_json(row["result"])
        return result if result else dict(row)

    @staticmethod
    def list_exports(collection_id: int) -> list[dict]:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM dataset_export WHERE collection_id = ? ORDER BY created_at DESC", (collection_id,)).fetchall()
        conn.close()
        return [_safe_json(row["result"]) or dict(row) for row in rows]
