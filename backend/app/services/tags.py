"""Ground-truth tag curation without mutating provider provenance."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.db import get_connection

TAG_CATEGORIES = ("artist", "character", "copyright", "species", "general", "meta")
TAG_CATEGORY_ORDER = {category: index for index, category in enumerate(TAG_CATEGORIES)}
DEFAULT_GROUND_TRUTH_CATEGORIES = ("artist", "character", "copyright", "species", "general")
GROUND_TRUTH_TAG_PROVIDERS = frozenset({"danbooru", "gelbooru", "e621"})
QUALITY_MARKS = ("masterpiece", "best quality", "low quality")
AESTHETIC_MARKS = ("very aesthetic", "aesthetic")


def normalize_tag(tag: str) -> str:
    """Normalize booru/user spelling to the trainer-facing space format."""
    return " ".join(str(tag).replace("_", " ").strip().strip(",").split())


def normalize_tags(tags: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        tag = normalize_tag(raw_tag)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


class TagService:
    def __init__(self, library_path: Path):
        self.library_path = Path(library_path)
        self.images_path = self.library_path / "images"

    def get_ground_truth(self, image_id: int) -> dict:
        conn = get_connection()
        if not conn.execute("SELECT 1 FROM image WHERE id = ?", (image_id,)).fetchone():
            conn.close()
            raise LookupError("Image not found")
        included_categories = self._included_categories(conn, image_id)
        source_tags = self._source_tags(conn, image_id, included_categories)
        overrides = self._override_rows(conn, image_id)
        tags = self._effective_tags(conn, image_id)
        quality_tags = self._quality_tags(conn, image_id)
        conn.close()
        return {
            "image_id": image_id,
            "tags": tags,
            "quality_tags": quality_tags,
            "source_tags": source_tags,
            "overrides": overrides,
            "included_categories": included_categories,
        }

    @staticmethod
    def _validate_categories(categories: Iterable[str]) -> list[str]:
        requested = {str(category).strip().lower() for category in categories}
        unknown = requested - set(TAG_CATEGORIES)
        if unknown:
            raise ValueError(f"Unknown tag categories: {', '.join(sorted(unknown))}")
        return [category for category in TAG_CATEGORIES if category in requested]

    def get_global_category_policy(self) -> dict:
        conn = get_connection()
        row = conn.execute("SELECT value FROM app_setting WHERE key = 'ground_truth_categories'").fetchone()
        conn.close()
        categories = self._validate_categories(json.loads(row[0])) if row else list(DEFAULT_GROUND_TRUTH_CATEGORIES)
        return {"categories": categories, "available_categories": list(TAG_CATEGORIES)}

    def set_global_category_policy(self, categories: Iterable[str]) -> dict:
        categories = self._validate_categories(categories)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        conn.execute(
            """INSERT INTO app_setting (key, value, updated_at) VALUES ('ground_truth_categories', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (json.dumps(categories), now),
        )
        image_ids = [row[0] for row in conn.execute(
            """SELECT i.id FROM image i LEFT JOIN collection c ON c.id = i.folder_id
               WHERE c.ground_truth_categories IS NULL OR i.folder_id IS NULL"""
        ).fetchall()]
        conn.commit()
        conn.close()
        warnings = self._rewrite_sidecars(image_ids)
        return {"categories": categories, "available_categories": list(TAG_CATEGORIES), "updated_images": len(image_ids), "warnings": warnings}

    def get_folder_category_policy(self, folder_id: int) -> dict:
        conn = get_connection()
        row = conn.execute("SELECT ground_truth_categories FROM collection WHERE id = ?", (folder_id,)).fetchone()
        if not row:
            conn.close()
            raise LookupError("Folder not found")
        global_row = conn.execute("SELECT value FROM app_setting WHERE key = 'ground_truth_categories'").fetchone()
        global_categories = self._validate_categories(json.loads(global_row[0])) if global_row else list(DEFAULT_GROUND_TRUTH_CATEGORIES)
        custom = self._validate_categories(json.loads(row[0])) if row[0] is not None else None
        conn.close()
        return {
            "folder_id": folder_id,
            "inherits_global": custom is None,
            "categories": custom,
            "effective_categories": custom if custom is not None else global_categories,
            "global_categories": global_categories,
            "available_categories": list(TAG_CATEGORIES),
        }

    def set_folder_category_policy(self, folder_id: int, categories: Optional[Iterable[str]]) -> dict:
        custom = self._validate_categories(categories) if categories is not None else None
        conn = get_connection()
        if not conn.execute("SELECT 1 FROM collection WHERE id = ?", (folder_id,)).fetchone():
            conn.close()
            raise LookupError("Folder not found")
        conn.execute(
            "UPDATE collection SET ground_truth_categories = ?, updated_at = ? WHERE id = ?",
            (json.dumps(custom) if custom is not None else None, datetime.now(timezone.utc).isoformat(), folder_id),
        )
        image_ids = [row[0] for row in conn.execute("SELECT id FROM image WHERE folder_id = ?", (folder_id,)).fetchall()]
        conn.commit()
        conn.close()
        warnings = self._rewrite_sidecars(image_ids)
        result = self.get_folder_category_policy(folder_id)
        result.update({"updated_images": len(image_ids), "warnings": warnings})
        return result

    def rewrite_all_sidecars(self) -> list[str]:
        conn = get_connection()
        image_ids = [row[0] for row in conn.execute("SELECT id FROM image WHERE folder_id IS NOT NULL").fetchall()]
        conn.close()
        return self._rewrite_sidecars(image_ids)

    def _rewrite_sidecars(self, image_ids: Iterable[int]) -> list[str]:
        return [warning for image_id in image_ids if (warning := self._rewrite_sidecar(int(image_id)))]

    def replace_ground_truth(self, image_id: int, tags: Iterable[str]) -> dict:
        desired = normalize_tags(tags)
        conn = get_connection()
        if not conn.execute("SELECT 1 FROM image WHERE id = ?", (image_id,)).fetchone():
            conn.close()
            raise LookupError("Image not found")
        # Committed quality tags are managed by the marks, not by overrides.
        managed = {tag.casefold() for tag in self._quality_tags(conn, image_id)}
        desired = [tag for tag in desired if tag.casefold() not in managed]
        before = self._override_rows(conn, image_id)
        source = self._source_tags(conn, image_id, self._included_categories(conn, image_id))
        source_keys = {tag.casefold(): tag for tag in source}
        desired_keys = {tag.casefold(): tag for tag in desired}
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM image_tag_override WHERE image_id = ?", (image_id,))
        for key, tag in desired_keys.items():
            if key not in source_keys:
                conn.execute(
                    "INSERT INTO image_tag_override (image_id, tag, action, category, updated_at, position) VALUES (?, ?, 'add', 'general', ?, ?)",
                    (image_id, tag, now, list(desired_keys).index(key)),
                )
        for key, tag in source_keys.items():
            if key not in desired_keys:
                conn.execute(
                    "INSERT INTO image_tag_override (image_id, tag, action, category, updated_at) VALUES (?, ?, 'remove', 'general', ?)",
                    (image_id, tag, now),
                )
        after = self._override_rows(conn, image_id)
        token = self._record_operation(conn, None, "replace", {image_id: before}) if before != after else None
        conn.commit()
        conn.close()
        warning = self._rewrite_sidecar(image_id)
        result = self.get_ground_truth(image_id)
        result.update({"changed": before != after, "undo_token": token, "warning": warning})
        return result

    def bulk_edit(self, collection_id: int, image_ids: Iterable[int], tags: Iterable[str], action: str) -> dict:
        if action not in {"add", "remove"}:
            raise ValueError("Action must be add or remove")
        normalized = normalize_tags(tags)
        if not normalized:
            raise ValueError("Enter at least one tag")
        requested_ids = list(dict.fromkeys(int(image_id) for image_id in image_ids))
        if not requested_ids:
            raise ValueError("Select at least one image")
        conn = get_connection()
        if not conn.execute("SELECT 1 FROM collection WHERE id = ?", (collection_id,)).fetchone():
            conn.close()
            raise LookupError("Folder not found")
        placeholders = ",".join("?" for _ in requested_ids)
        member_ids = {row[0] for row in conn.execute(
            f"SELECT image_id FROM collection_image WHERE collection_id = ? AND image_id IN ({placeholders})",
            [collection_id, *requested_ids],
        ).fetchall()}
        if member_ids != set(requested_ids):
            conn.close()
            raise ValueError("Every selected image must belong to this folder")

        before = {image_id: self._override_rows(conn, image_id) for image_id in requested_ids}
        now = datetime.now(timezone.utc).isoformat()
        for image_id in requested_ids:
            source_keys = {tag.casefold() for tag in self._source_tags(conn, image_id, self._included_categories(conn, image_id))}
            effective_keys = {tag.casefold() for tag in self._effective_tags(conn, image_id)}
            for tag in normalized:
                key = tag.casefold()
                if action == "add":
                    if key in effective_keys:
                        continue
                    if key in source_keys:
                        conn.execute("DELETE FROM image_tag_override WHERE image_id = ? AND tag = ?", (image_id, tag))
                    else:
                        conn.execute(
                            """INSERT INTO image_tag_override (image_id, tag, action, category, updated_at)
                               VALUES (?, ?, 'add', 'general', ?)
                               ON CONFLICT(image_id, tag) DO UPDATE SET action = 'add', updated_at = excluded.updated_at""",
                            (image_id, tag, now),
                        )
                    effective_keys.add(key)
                else:
                    if key not in effective_keys:
                        continue
                    if key in source_keys:
                        conn.execute(
                            """INSERT INTO image_tag_override (image_id, tag, action, category, updated_at)
                               VALUES (?, ?, 'remove', 'general', ?)
                               ON CONFLICT(image_id, tag) DO UPDATE SET action = 'remove', updated_at = excluded.updated_at""",
                            (image_id, tag, now),
                        )
                    else:
                        conn.execute("DELETE FROM image_tag_override WHERE image_id = ? AND tag = ?", (image_id, tag))
                    effective_keys.discard(key)

        changed_ids = [
            image_id for image_id in requested_ids
            if before[image_id] != self._override_rows(conn, image_id)
        ]
        changed_before = {image_id: before[image_id] for image_id in changed_ids}
        token = self._record_operation(conn, collection_id, action, changed_before) if changed_ids else None
        conn.commit()
        conn.close()
        warnings = [warning for image_id in changed_ids if (warning := self._rewrite_sidecar(image_id))]
        return {
            "action": action,
            "requested_images": len(requested_ids),
            "changed_images": len(changed_ids),
            "tags": normalized,
            "undo_token": token,
            "warnings": warnings,
        }

    def bulk_replace(
        self,
        collection_id: int,
        image_ids: Iterable[int],
        old_tag: str,
        new_tag: str,
    ) -> dict:
        """Replace one effective tag on selected collection images.

        Provider tags remain immutable. A source tag is hidden with a remove
        override and the replacement is restored/added through the same
        override layer used by individual and batch edits.
        """
        old_tag = normalize_tag(old_tag)
        new_tag = normalize_tag(new_tag)
        if not old_tag or not new_tag:
            raise ValueError("Enter both the tag to replace and its replacement")
        if old_tag.casefold() == new_tag.casefold():
            raise ValueError("The replacement tag must be different")
        requested_ids = list(dict.fromkeys(int(image_id) for image_id in image_ids))
        if not requested_ids:
            raise ValueError("Select at least one image")

        conn = get_connection()
        if not conn.execute("SELECT 1 FROM collection WHERE id = ?", (collection_id,)).fetchone():
            conn.close()
            raise LookupError("Folder not found")
        placeholders = ",".join("?" for _ in requested_ids)
        member_ids = {row[0] for row in conn.execute(
            f"SELECT image_id FROM collection_image WHERE collection_id = ? AND image_id IN ({placeholders})",
            [collection_id, *requested_ids],
        ).fetchall()}
        if member_ids != set(requested_ids):
            conn.close()
            raise ValueError("Every selected image must belong to this folder")

        before = {image_id: self._override_rows(conn, image_id) for image_id in requested_ids}
        old_key = old_tag.casefold()
        matched_ids: list[int] = []
        now = datetime.now(timezone.utc).isoformat()
        for image_id in requested_ids:
            effective = self._effective_tags(conn, image_id)
            effective_keys = {tag.casefold() for tag in effective}
            if old_key not in effective_keys:
                # Backward compatibility for replacements created by the
                # first batch-replace build: it stored a same-timestamp remove
                # + unpositioned add pair, which appended the new tag. Let the
                # user rerun the same replacement once to repair those rows.
                overrides = self._override_rows(conn, image_id)
                old_override = next((row for row in overrides if row["tag"].casefold() == old_key and row["action"] == "remove"), None)
                new_override = next((row for row in overrides if row["tag"].casefold() == new_tag.casefold() and row["action"] == "add"), None)
                source = self._source_tags(conn, image_id, self._included_categories(conn, image_id))
                source_position = next((index for index, tag in enumerate(source) if tag.casefold() == old_key), None)
                if not (
                    old_override and new_override and new_override["position"] is None
                    and old_override["updated_at"] == new_override["updated_at"]
                    and source_position is not None
                ):
                    continue
                replacement_position = source_position
            else:
                replacement_position = next(
                    index for index, tag in enumerate(effective) if tag.casefold() == old_key
                )
            matched_ids.append(image_id)
            included_categories = self._included_categories(conn, image_id)
            source_keys = {tag.casefold() for tag in self._source_tags(conn, image_id, included_categories)}
            replacement_category = self._category_for_tag(conn, image_id, old_tag, included_categories)

            if old_key in source_keys:
                conn.execute(
                    """INSERT INTO image_tag_override (image_id, tag, action, category, updated_at)
                       VALUES (?, ?, 'remove', 'general', ?)
                       ON CONFLICT(image_id, tag) DO UPDATE SET action = 'remove', updated_at = excluded.updated_at""",
                    (image_id, old_tag, now),
                )
            else:
                conn.execute(
                    "DELETE FROM image_tag_override WHERE image_id = ? AND tag = ?",
                    (image_id, old_tag),
                )

            # An add override with a position also acts as a stable ordering
            # marker when the replacement already exists in source metadata.
            # _effective_tags removes that occurrence before reinserting it.
            conn.execute(
                """INSERT INTO image_tag_override
                   (image_id, tag, action, category, updated_at, position)
                   VALUES (?, ?, 'add', ?, ?, ?)
                   ON CONFLICT(image_id, tag) DO UPDATE SET
                       action = 'add', updated_at = excluded.updated_at,
                       position = excluded.position""",
                (image_id, new_tag, replacement_category, now, replacement_position),
            )

        changed_ids = [
            image_id for image_id in matched_ids
            if before[image_id] != self._override_rows(conn, image_id)
        ]
        changed_before = {image_id: before[image_id] for image_id in changed_ids}
        token = self._record_operation(conn, collection_id, "replace", changed_before) if changed_ids else None
        conn.commit()
        conn.close()
        warnings = [warning for image_id in changed_ids if (warning := self._rewrite_sidecar(image_id))]
        return {
            "action": "replace",
            "requested_images": len(requested_ids),
            "matched_images": len(matched_ids),
            "changed_images": len(changed_ids),
            "old_tag": old_tag,
            "new_tag": new_tag,
            "undo_token": token,
            "warnings": warnings,
        }

    def undo(self, token: str) -> dict:
        conn = get_connection()
        operation = conn.execute(
            "SELECT * FROM tag_edit_operation WHERE token = ?", (token,)
        ).fetchone()
        if not operation:
            conn.close()
            raise LookupError("Tag edit undo action not found")
        if operation["undone_at"]:
            conn.close()
            raise ValueError("This tag edit has already been undone")
        payload = json.loads(operation["payload"])
        restored_ids: list[int] = []
        for item in payload["images"]:
            image_id = int(item["image_id"])
            if not conn.execute("SELECT 1 FROM image WHERE id = ?", (image_id,)).fetchone():
                continue
            conn.execute("DELETE FROM image_tag_override WHERE image_id = ?", (image_id,))
            for override in item["overrides"]:
                conn.execute(
                    """INSERT INTO image_tag_override
                       (image_id, tag, action, category, updated_at, position)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (image_id, override["tag"], override["action"], override["category"], override["updated_at"], override.get("position")),
                )
            restored_ids.append(image_id)
        conn.execute(
            "UPDATE tag_edit_operation SET undone_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), operation["id"]),
        )
        conn.commit()
        conn.close()
        warnings = [warning for image_id in restored_ids if (warning := self._rewrite_sidecar(image_id))]
        return {"restored_images": len(restored_ids), "warnings": warnings}

    def collection_ground_truth_tags(self, collection_id: int) -> list[dict]:
        conn = get_connection()
        exists = conn.execute("SELECT 1 FROM collection WHERE id = ?", (collection_id,)).fetchone()
        if not exists:
            conn.close()
            raise LookupError("Folder not found")
        image_ids = [row[0] for row in conn.execute(
            "SELECT image_id FROM collection_image WHERE collection_id = ?", (collection_id,)
        ).fetchall()]
        display: dict[str, str] = {}
        counts: Counter[str] = Counter()
        for image_id in image_ids:
            for tag in self._effective_tags(conn, image_id):
                key = tag.casefold()
                display.setdefault(key, tag)
                counts[key] += 1
        conn.close()
        return [
            {"category": "ground truth", "tag": display[key], "count": count}
            for key, count in counts.most_common()
        ]

    @staticmethod
    def _source_tags(conn, image_id: int, included_categories: Optional[Iterable[str]] = None) -> list[str]:
        categories = list(included_categories) if included_categories is not None else None
        if categories is not None and not categories:
            return []

        # The folder name is the canonical artist identity for training. Site
        # profile display names may contain status text, mixed casing, or
        # aliases, so provider artist tags never define the trigger.
        folder = conn.execute(
            """SELECT c.name, c.artist_tag_template, c.type
               FROM image i JOIN collection c ON c.id = i.folder_id
               WHERE i.id = ?""",
            (image_id,),
        ).fetchone()
        tags: list[str] = []
        if folder and folder[2] == "artist" and folder[1] and (categories is None or "artist" in categories):
            artist_name = normalize_tag(folder[0]).casefold()
            if artist_name:
                tags.append(normalize_tag(str(folder[1]).replace("{artist}", artist_name)))

        # Only the three curated booru taxonomies are suitable as automatic
        # trainer ground truth. Every other provider's tags remain intact in
        # image_tag/image_source and visible as read-only provenance.
        source_categories = [category for category in (categories or TAG_CATEGORIES) if category != "artist" or (folder and folder[2] != "artist")]
        if not source_categories:
            return normalize_tags(tags)
        trusted = sorted(GROUND_TRUTH_TAG_PROVIDERS)
        params: list = [image_id, *trusted, *source_categories]
        rows = conn.execute(
            f"""SELECT it.category, it.tag
               FROM image_tag it
               JOIN image_source src ON src.id = it.source_id
               WHERE it.image_id = ?
                 AND src.provider IN ({','.join('?' for _ in trusted)})
                 AND it.category IN ({','.join('?' for _ in source_categories)})
               ORDER BY CASE it.category
                    WHEN 'artist' THEN 0 WHEN 'character' THEN 1 WHEN 'copyright' THEN 2
                    WHEN 'species' THEN 3 WHEN 'general' THEN 4 WHEN 'meta' THEN 5 ELSE 6 END, it.rowid""",
            params,
        ).fetchall()
        if "general" in source_categories:
            # Tagger additions (machine_tags.py) follow the post's own general tags; booru tags win duplicates.
            machine = conn.execute(
                "SELECT tag FROM image_machine_tag WHERE image_id = ? ORDER BY model, confidence DESC, tag", (image_id,)
            ).fetchall()
            if machine:
                rows = sorted([(row[0], row[1]) for row in rows] + [("general", row[0]) for row in machine],
                              key=lambda row: TAG_CATEGORY_ORDER.get(row[0], len(TAG_CATEGORY_ORDER)))
        for row in rows:
            tags.append(normalize_tag(row[1]))
        return normalize_tags(tags)

    @staticmethod
    def _category_for_tag(conn, image_id: int, tag: str, included_categories: Iterable[str]) -> str:
        categories = list(included_categories)
        folder = conn.execute(
            """SELECT c.name, c.artist_tag_template, c.type
               FROM image i JOIN collection c ON c.id = i.folder_id
               WHERE i.id = ?""",
            (image_id,),
        ).fetchone()
        if folder and folder[2] == "artist" and folder[1]:
            trigger = normalize_tag(str(folder[1]).replace("{artist}", normalize_tag(folder[0]).casefold()))
            if trigger.casefold() == normalize_tag(tag).casefold():
                return "artist"
        if categories:
            trusted = sorted(GROUND_TRUTH_TAG_PROVIDERS)
            row = conn.execute(
                f"""SELECT it.category FROM image_tag it
                    JOIN image_source src ON src.id = it.source_id
                    WHERE it.image_id = ? AND REPLACE(it.tag, '_', ' ') = ? COLLATE NOCASE
                      AND src.provider IN ({','.join('?' for _ in trusted)})
                      AND it.category IN ({','.join('?' for _ in categories)})
                    ORDER BY CASE it.category
                        WHEN 'artist' THEN 0 WHEN 'character' THEN 1 WHEN 'copyright' THEN 2
                        WHEN 'species' THEN 3 WHEN 'general' THEN 4 WHEN 'meta' THEN 5 ELSE 6 END,
                        it.rowid LIMIT 1""",
                [image_id, tag, *trusted, *categories],
            ).fetchone()
            if row:
                return row[0]
        return "general"

    @staticmethod
    def _source_category_map(conn, image_id: int, included_categories: Iterable[str]) -> dict[str, str]:
        categories = list(included_categories)
        result: dict[str, str] = {}
        folder = conn.execute(
            """SELECT c.name, c.artist_tag_template, c.type
               FROM image i JOIN collection c ON c.id = i.folder_id
               WHERE i.id = ?""",
            (image_id,),
        ).fetchone()
        if folder and folder[2] == "artist" and folder[1] and "artist" in categories:
            trigger = normalize_tag(str(folder[1]).replace("{artist}", normalize_tag(folder[0]).casefold()))
            if trigger:
                result[trigger.casefold()] = "artist"
        source_categories = [category for category in categories if category != "artist" or (folder and folder[2] != "artist")]
        if not source_categories:
            return result
        trusted = sorted(GROUND_TRUTH_TAG_PROVIDERS)
        rows = conn.execute(
            f"""SELECT it.category, it.tag FROM image_tag it
                JOIN image_source src ON src.id = it.source_id
                WHERE it.image_id = ?
                  AND src.provider IN ({','.join('?' for _ in trusted)})
                  AND it.category IN ({','.join('?' for _ in source_categories)})
                ORDER BY CASE it.category
                    WHEN 'artist' THEN 0 WHEN 'character' THEN 1 WHEN 'copyright' THEN 2
                    WHEN 'species' THEN 3 WHEN 'general' THEN 4 WHEN 'meta' THEN 5 ELSE 6 END,
                    it.rowid""",
            [image_id, *trusted, *source_categories],
        ).fetchall()
        for row in rows:
            result.setdefault(normalize_tag(row[1]).casefold(), row[0])
        return result

    @staticmethod
    def _included_categories(conn, image_id: int) -> list[str]:
        row = conn.execute(
            """SELECT c.ground_truth_categories
               FROM image i LEFT JOIN collection c ON c.id = i.folder_id
               WHERE i.id = ?""",
            (image_id,),
        ).fetchone()
        if row and row[0] is not None:
            return TagService._validate_categories(json.loads(row[0]))
        global_row = conn.execute("SELECT value FROM app_setting WHERE key = 'ground_truth_categories'").fetchone()
        return TagService._validate_categories(json.loads(global_row[0])) if global_row else list(DEFAULT_GROUND_TRUTH_CATEGORIES)

    @staticmethod
    def _override_rows(conn, image_id: int) -> list[dict]:
        return [dict(row) for row in conn.execute(
            """SELECT tag, action, category, updated_at, position FROM image_tag_override
               WHERE image_id = ? ORDER BY rowid""",
            (image_id,),
        ).fetchall()]

    def _effective_tags(self, conn, image_id: int) -> list[str]:
        included_categories = self._included_categories(conn, image_id)
        source = self._source_tags(conn, image_id, included_categories)
        overrides = self._override_rows(conn, image_id)
        removed = {row["tag"].casefold() for row in overrides if row["action"] == "remove"}
        additions = [
            row for row in overrides
            if row["action"] == "add" and row["category"] in included_categories
        ]
        addition_keys = {row["tag"].casefold() for row in additions}
        # A positioned add may refer to an immutable source tag. Exclude its
        # original occurrence so it can be reinserted at exactly one position.
        result = [
            tag for tag in source
            if tag.casefold() not in removed and tag.casefold() not in addition_keys
        ]
        positioned = sorted(
            (row for row in additions if row["position"] is not None),
            key=lambda row: (int(row["position"]), row["tag"].casefold()),
        )
        unpositioned = [row for row in additions if row["position"] is None]
        for row in positioned:
            position = min(max(int(row["position"]), 0), len(result))
            result.insert(position, row["tag"])
        result_keys = {tag.casefold() for tag in result}
        for row in unpositioned:
            key = row["tag"].casefold()
            if key not in result_keys:
                result.append(row["tag"])
                result_keys.add(key)
        # Category grouping is a trainer-facing invariant. The sort is stable,
        # so provider order and explicit replacement positions are retained
        # within their category while meta can never drift ahead of general.
        addition_categories = {
            row["tag"].casefold(): row["category"] for row in additions
        }
        source_categories = self._source_category_map(conn, image_id, included_categories)
        result = sorted(
            result,
            key=lambda tag: TAG_CATEGORY_ORDER.get(
                addition_categories.get(tag.casefold())
                or source_categories.get(tag.casefold(), "general"),
                len(TAG_CATEGORY_ORDER),
            ),
        )
        # Quality and aesthetic tags committed by accepting the folder always
        # come last, after every other category.
        quality = self._quality_tags(conn, image_id)
        if quality:
            keys = {tag.casefold() for tag in quality}
            result = [tag for tag in result if tag.casefold() not in keys] + quality
        return result

    @staticmethod
    def _quality_tags(conn, image_id: int) -> list[str]:
        row = conn.execute("SELECT quality_tags FROM image WHERE id = ?", (image_id,)).fetchone()
        try:
            return normalize_tags(json.loads(row[0])) if row and row[0] else []
        except (TypeError, ValueError):
            return []

    @staticmethod
    def _record_operation(conn, collection_id: Optional[int], action: str, snapshots: dict[int, list[dict]]) -> str:
        token = uuid.uuid4().hex
        payload = {
            "images": [
                {"image_id": image_id, "overrides": overrides}
                for image_id, overrides in snapshots.items()
            ]
        }
        conn.execute(
            """INSERT INTO tag_edit_operation (token, collection_id, action, payload, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token, collection_id, action, json.dumps(payload, sort_keys=True), datetime.now(timezone.utc).isoformat()),
        )
        return token

    def _rewrite_sidecar(self, image_id: int) -> Optional[str]:
        conn = get_connection()
        row = conn.execute("SELECT path FROM image WHERE id = ?", (image_id,)).fetchone()
        if not row:
            conn.close()
            return f"Image {image_id} no longer exists"
        tags = self._effective_tags(conn, image_id)
        conn.close()
        root = self.images_path.resolve()
        image_path = (root / row["path"]).resolve()
        if root not in image_path.parents:
            return f"Unsafe image path for image {image_id}"
        if not image_path.exists():
            return f"Image file is missing for image {image_id}"
        sidecar_path = image_path.with_suffix(".txt")
        temp_path = sidecar_path.with_name(f".{sidecar_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(", ".join(tags), encoding="utf-8")
            temp_path.replace(sidecar_path)
        except OSError as exc:
            return f"Could not write sidecar for image {image_id}: {exc}"
        finally:
            temp_path.unlink(missing_ok=True)
        return None
