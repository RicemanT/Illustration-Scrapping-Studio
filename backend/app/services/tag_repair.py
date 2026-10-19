"""Startup repair: Gelbooru tag names that were stored HTML-escaped.

Gelbooru's API returns tag names HTML-escaped (``bang_dream!_it&#039;s_mygo!!!!!``, ``&amp;``). Before the provider
unescaped them, the Studio stored them as such, and the escaped names never matched Gelbooru's tag index, so their
category fell back to ``general`` (a series showing up as a general tag). This restores the names everywhere they
were stored -- the Studio's tag rows and tag edits, the planner's harvested posts -- and re-files them under their
real category, taken from Danbooru, which uses the same tag names (the harvested Danbooru posts, then the Studio's
own Danbooru tag rows). Ground-truth sidecars of the affected images are rewritten. Once nothing escaped is left,
it is a few cheap queries.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from app import db

ESCAPED = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z]+);")
PLANNER_COLUMNS = {"artists": "artist", "characters": "character", "copyrights": "copyright",
                   "species": "species", "general": "general", "meta": "meta"}
COLUMN_OF = {category: column for column, category in PLANNER_COLUMNS.items()}


def _escaped(tag: str) -> bool:
    return bool(ESCAPED.search(tag or ""))


def _danbooru_categories(names: set[str], planner, main) -> dict[str, str]:
    """The non-general category of each name, from Danbooru (same tag vocabulary as Gelbooru)."""
    found: dict[str, str] = {}
    if names and planner is not None:
        columns = [column for column in PLANNER_COLUMNS if column != "general"]
        for row in planner.execute(f"SELECT {', '.join(columns)} FROM post WHERE site = 'danbooru'"):
            for column, value in zip(columns, row):
                for tag in (value or "").split():
                    if tag in names and tag not in found:
                        found[tag] = PLANNER_COLUMNS[column]
            if len(found) == len(names):
                break
    for name in names - set(found):
        row = main.execute(
            """SELECT it.category FROM image_tag it JOIN image_source s ON s.id = it.source_id
               WHERE s.provider = 'danbooru' AND it.tag = ? AND it.category != 'general' LIMIT 1""", (name,)).fetchone()
        if row:
            found[name] = row[0]
    return found


def repair_escaped_gelbooru_tags(library_path: Path) -> dict:
    stats = {"tag_rows": 0, "recategorized": 0, "overrides": 0, "planner_posts": 0, "sidecars": 0, "unresolved": []}
    main = db.get_connection()
    try:
        tag_rows = [row for row in main.execute(
            """SELECT it.rowid, it.image_id, it.category, it.tag FROM image_tag it
               JOIN image_source s ON s.id = it.source_id
               WHERE s.provider = 'gelbooru' AND it.tag LIKE '%&%;%'""").fetchall() if _escaped(row[3])]
        override_rows = [row for row in main.execute(
            "SELECT image_id, tag FROM image_tag_override WHERE tag LIKE '%&%;%'").fetchall() if _escaped(row[1])]
    finally:
        main.close()

    planner = None
    planner_rows = []
    try:
        from app.services import planner_store
        planner = planner_store.connect()
        like = " OR ".join(f"{column} LIKE '%&%;%'" for column in PLANNER_COLUMNS)
        planner_rows = planner.execute(
            f"SELECT artist_id, remote_id, {', '.join(PLANNER_COLUMNS)} FROM post WHERE site = 'gelbooru' AND ({like})"
        ).fetchall()
    except Exception:   # no planner database: only the Studio's own rows
        planner = None

    if not (tag_rows or override_rows or planner_rows):
        if planner is not None:
            planner.close()
        return stats

    # Every escaped name lost its category (the lookup missed it), wherever it was filed.
    names = {html.unescape(row[3]) for row in tag_rows}
    for row in planner_rows:
        for value in row[2:]:
            names.update(html.unescape(tag) for tag in (value or "").split() if _escaped(tag))
    main = db.get_connection()
    try:
        known = _danbooru_categories(names, planner, main)
        stats["unresolved"] = sorted(name for name in names if name not in known)

        affected = set()
        for rowid, image_id, category, tag in tag_rows:
            name = html.unescape(tag)
            new_category = known.get(name, category) if category == "general" else category
            stats["recategorized"] += new_category != category
            main.execute("UPDATE image_tag SET tag = ?, category = ? WHERE rowid = ?", (name, new_category, rowid))
            affected.add(image_id)
        stats["tag_rows"] = len(tag_rows)
        for image_id, tag in override_rows:
            # An edit may already exist under the clean name: then the escaped duplicate just goes.
            main.execute("UPDATE OR IGNORE image_tag_override SET tag = ? WHERE image_id = ? AND tag = ?",
                         (html.unescape(tag), image_id, tag))
            main.execute("DELETE FROM image_tag_override WHERE image_id = ? AND tag = ?", (image_id, tag))
            affected.add(image_id)
        stats["overrides"] = len(override_rows)
        main.commit()
    finally:
        main.close()

    if planner is not None:
        try:
            for row in planner_rows:
                columns = {column: (value or "").split() for column, value in zip(PLANNER_COLUMNS, row[2:])}
                fixed = {column: [] for column in PLANNER_COLUMNS}
                for column, tags in columns.items():
                    for tag in tags:
                        if not _escaped(tag):
                            fixed[column].append(tag)
                            continue
                        name = html.unescape(tag)
                        target = COLUMN_OF.get(known.get(name), column) if column == "general" else column
                        if name not in fixed[target]:
                            fixed[target].append(name)
                planner.execute(
                    f"UPDATE post SET {', '.join(column + ' = ?' for column in PLANNER_COLUMNS)} "
                    "WHERE site = 'gelbooru' AND artist_id = ? AND remote_id = ?",
                    [" ".join(fixed[column]) for column in PLANNER_COLUMNS] + [row[0], row[1]])
            planner.commit()
            stats["planner_posts"] = len(planner_rows)
        finally:
            planner.close()

    if affected:
        from app.services.tags import TagService
        ids, in_folders = sorted(affected), []
        main = db.get_connection()
        try:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                in_folders += [row[0] for row in main.execute(
                    f"SELECT id FROM image WHERE folder_id IS NOT NULL AND id IN ({','.join('?' * len(chunk))})",
                    chunk).fetchall()]
        finally:
            main.close()
        TagService(library_path)._rewrite_sidecars(in_folders)
        stats["sidecars"] = len(in_folders)
    return stats
