import sqlite3
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import LIBRARY_PATH, get_connection
from app.services import groups

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
