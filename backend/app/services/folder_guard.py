"""Keeps hand curation from being undone by automatic imports.

Two rules, enforced where images are written (so every path obeys them: Sync, Sync All, scheduled and resumed
syncs, Dataset Planner deliveries and import batches):

* An accepted folder (`collection.frozen_at`, set when a planner folder is accepted) takes no new images at all.
  Reopen the folder to change it.
* A post removed from a folder by hand is remembered (`folder_removed_source`). Automatic imports (sync,
  delivery) never bring it back; that includes video and animation frames, so deleting some frames of a post does
  not make the next sync extract the missing ones again. Importing the post yourself (an import batch, or
  accepting it as a wildcard) clears the record. Undoing the removal clears it too.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


class FolderFrozen(RuntimeError):
    """The folder is accepted; it takes no new images until it is reopened."""


def _base(remote_id) -> str:
    """The post a source belongs to: `123:frame:2` -> `123`."""
    return str(remote_id).split(':', 1)[0]


def frozen_at(conn, folder_id: int):
    row = conn.execute('SELECT frozen_at FROM collection WHERE id=?', (folder_id,)).fetchone()
    return row[0] if row else None


def require_open(conn, folder_id: int) -> None:
    if frozen_at(conn, folder_id):
        raise FolderFrozen('This folder is accepted; it takes no new images. Reopen it to add images.')


def set_frozen(conn, folder_id: int, stamp) -> None:
    conn.execute('UPDATE collection SET frozen_at=? WHERE id=?', (stamp, folder_id))


def record_removed(conn, folder_id: int, sources: Iterable[tuple]) -> int:
    """Remember removed sources, each as itself and as its post, so the post is not imported again."""
    stamp = datetime.now(timezone.utc).isoformat()
    rows = set()
    for provider, remote_id in sources:
        rows.add((folder_id, provider, str(remote_id), stamp))
        rows.add((folder_id, provider, _base(remote_id), stamp))
    conn.executemany('INSERT OR IGNORE INTO folder_removed_source (folder_id, provider, remote_id, removed_at) VALUES (?,?,?,?)',
                     sorted(rows))
    return len(rows)


def record_removed_images(conn, folder_id: int, image_ids: list[int]) -> int:
    if not image_ids:
        return 0
    placeholders = ','.join('?' * len(image_ids))
    sources = conn.execute(f'SELECT provider, remote_id FROM image_source WHERE image_id IN ({placeholders})', image_ids).fetchall()
    return record_removed(conn, folder_id, [(r[0], r[1]) for r in sources])


def clear_restored_images(conn, folder_id: int, image_ids: list[int]) -> None:
    """Undo of a removal: forget exactly the restored sources, and a post once none of its frames is still removed."""
    if not image_ids:
        return
    placeholders = ','.join('?' * len(image_ids))
    sources = conn.execute(f'SELECT provider, remote_id FROM image_source WHERE image_id IN ({placeholders})', image_ids).fetchall()
    for provider, remote_id in sources:
        conn.execute('DELETE FROM folder_removed_source WHERE folder_id=? AND provider=? AND remote_id=?', (folder_id, provider, str(remote_id)))
    for provider, base in {(r[0], _base(r[1])) for r in sources}:
        still = conn.execute("""SELECT 1 FROM folder_removed_source WHERE folder_id=? AND provider=? AND remote_id LIKE ?
                                LIMIT 1""", (folder_id, provider, base + ':%')).fetchone()
        if not still:
            conn.execute('DELETE FROM folder_removed_source WHERE folder_id=? AND provider=? AND remote_id=?', (folder_id, provider, base))


def clear_posts(conn, folder_id: int, provider: str, remote_ids: Iterable) -> None:
    """You imported these posts yourself: they and all their frames may come back."""
    for remote_id in {_base(r) for r in remote_ids}:
        conn.execute('DELETE FROM folder_removed_source WHERE folder_id=? AND provider=? AND (remote_id=? OR remote_id LIKE ?)',
                     (folder_id, provider, remote_id, remote_id + ':%'))


def removed_post(conn, folder_id: int, provider: str, remote_id) -> bool:
    return conn.execute('SELECT 1 FROM folder_removed_source WHERE folder_id=? AND provider=? AND remote_id=?',
                        (folder_id, provider, _base(remote_id))).fetchone() is not None


def skip_reason(conn, folder_id: int, provider: str, remote_id, automatic: bool):
    """Why this post must not be imported into the folder now, or None."""
    if frozen_at(conn, folder_id):
        return f'Skipped {remote_id}: the folder is accepted (reopen it to add images)'
    if automatic and removed_post(conn, folder_id, provider, remote_id):
        return f'Skipped {remote_id}: removed from this folder by hand'
    return None


def _when(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def late_images(conn, accepted: dict[int, str]) -> dict[int, list[int]]:
    """Images added to accepted folders after they were accepted: {folder_id: [image_id]} (folders with any)."""
    if not accepted:
        return {}
    cutoff = {folder_id: _when(stamp) for folder_id, stamp in accepted.items()}
    found: dict[int, list[int]] = {}
    placeholders = ','.join('?' * len(cutoff))
    for image_id, folder_id, added_at in conn.execute(
            f'SELECT id, folder_id, added_at FROM image WHERE folder_id IN ({placeholders}) ORDER BY id', list(cutoff)):
        added, limit = _when(added_at), cutoff.get(folder_id)
        if added and limit and added > limit:
            found.setdefault(folder_id, []).append(image_id)
    return found
