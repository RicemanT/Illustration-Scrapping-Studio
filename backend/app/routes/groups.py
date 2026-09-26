import sqlite3
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import LIBRARY_PATH, get_connection
from app.services import groups
from datetime import datetime, timezone

router = APIRouter()


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str


class ArtistList(BaseModel):
    text: str = Field(max_length=1024 * 1024)
    type: str = "artist"
    additional_sources: list[str] = Field(default_factory=list, max_length=8)


class FolderMove(BaseModel):
    group_id: int | None = Field(default=None, gt=0)


def checked(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(409, f'Folder operation could not finish: {exc}. Retry after resolving the filesystem error.') from exc


@router.get('')
def list_groups():
    return groups.list_groups()


@router.post('', status_code=201)
def create_group(payload: GroupCreate):
    return checked(groups.create_group, payload.name, payload.provider, LIBRARY_PATH)


@router.post('/{group_id}/collections/preview')
@router.post('/{group_id}/artists/preview')
def preview_artists(group_id: int, payload: ArtistList):
    return checked(groups.import_artists, group_id, payload.text, kind=payload.type, additional_sources=payload.additional_sources)


@router.post('/{group_id}/collections')
@router.post('/{group_id}/artists')
def import_artists(group_id: int, payload: ArtistList):
    return checked(groups.import_artists, group_id, payload.text, apply=True, library=LIBRARY_PATH, kind=payload.type, additional_sources=payload.additional_sources)


@router.post('/folders/{folder_id}/move')
async def move_folder(folder_id: int, payload: FolderMove):
    checked(groups.move_folder, folder_id, payload.group_id, LIBRARY_PATH)
    return {'folder_id': folder_id, 'group_id': payload.group_id}


@router.delete('/{group_id}')
async def delete_group(group_id: int):
    if not checked(groups.delete_group, group_id, LIBRARY_PATH): raise HTTPException(404, 'Group not found')
    return {'status': 'deleted'}

@router.get('/{group_id}/blocked')
def list_blocked(group_id: int):
    conn = get_connection()
    try:
        if not conn.execute('SELECT 1 FROM artist_group WHERE id=?', (group_id,)).fetchone(): raise HTTPException(404, 'Group not found')
        rows = conn.execute('''SELECT c.id,c.name FROM group_blocked_folder b JOIN collection c ON c.id=b.folder_id WHERE b.group_id=? ORDER BY c.name COLLATE NOCASE''', (group_id,)).fetchall()
        return [dict(row) for row in rows]
    finally: conn.close()

class ProtectionUpdate(BaseModel):
    all: bool = False
    text: str = Field(default='', max_length=1024 * 1024)
    folder_ids: list[int] = Field(default_factory=list, max_length=5000)


@router.post('/{group_id}/blocked')
def add_blocked(group_id: int, payload: ProtectionUpdate):
    if len(payload.text.encode('utf-8')) > 1024 * 1024 or len(payload.text.splitlines()) > 5000:
        raise HTTPException(422, 'Use at most 5,000 lines and 1 MiB')
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        checked(groups.validate_group, conn, group_id)
        checked(groups.assert_idle, conn)
        members = conn.execute('SELECT id,name FROM collection WHERE group_id=?', (group_id,)).fetchall()
        names = {line.strip().casefold() for line in payload.text.lstrip('\ufeff').splitlines() if line.strip()}
        known = {row['name'].casefold() for row in members}
        unknown = sorted(names - known)
        valid_ids = {row['id'] for row in members}
        if unknown or set(payload.folder_ids) - valid_ids:
            raise HTTPException(422, 'No changes made. Unknown collection names or IDs: ' + ', '.join(unknown[:20]))
        ids = valid_ids if payload.all else set(payload.folder_ids) | {row['id'] for row in members if row['name'].casefold() in names}
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany('INSERT OR IGNORE INTO group_blocked_folder(group_id,folder_id,created_at) VALUES(?,?,?)', [(group_id, folder_id, now) for folder_id in ids])
        conn.commit()
        return {'blocked': conn.execute('SELECT COUNT(*) FROM group_blocked_folder WHERE group_id=?', (group_id,)).fetchone()[0]}
    finally:
        conn.close()

@router.delete('/{group_id}/blocked/{folder_id}')
def remove_blocked(group_id: int, folder_id: int):
    conn = get_connection()
    try:
        checked(groups.assert_idle, conn)
        conn.execute('DELETE FROM group_blocked_folder WHERE group_id=? AND folder_id=?',(group_id,folder_id)); conn.commit(); return {'status':'unblocked'}
    finally: conn.close()
@router.post('/{group_id}/folders/{folder_id}/enable-source')
def enable_group_source(group_id: int, folder_id: int):
    conn = get_connection()
    try:
        group = checked(groups.validate_group, conn, group_id)
        if not conn.execute('SELECT 1 FROM collection WHERE id=? AND group_id=?', (folder_id, group_id)).fetchone():
            raise HTTPException(404, 'Folder not found in this group')
        folder = conn.execute('SELECT type,query FROM collection WHERE id=?', (folder_id,)).fetchone()
        from app.services.queries import validate_collection_query
        checked(validate_collection_query, folder['query'], folder['type'], group['provider'])
        conn.execute('''INSERT INTO collection_source(collection_id,provider,enabled) VALUES(?,?,1)
            ON CONFLICT(collection_id,provider) DO UPDATE SET enabled=1''', (folder_id, group['provider']))
        conn.commit()
        return {'folder_id': folder_id, 'provider': group['provider'], 'enabled': True}
    finally:
        conn.close()
