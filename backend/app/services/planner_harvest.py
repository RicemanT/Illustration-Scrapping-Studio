"""Dataset Planner harvest: collect post metadata for every planned artist.

No media is downloaded. Each site runs as its own loop with the provider's
normal request pacing, so Danbooru, Gelbooru and e621 progress in parallel.
Progress is committed after every page: a canceled or interrupted harvest
resumes from each artist's saved cursor.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from app.models import RemotePost
from app.providers.booru import BOORU_SITES, ProviderAuthenticationError
from app.providers.registry import create_provider
from app.services.planner_store import POST_COLUMNS, SITES, connect, harvest_row, now

RATINGS = {
    'danbooru': {'g': 'general', 's': 'sensitive', 'q': 'questionable', 'e': 'explicit'},
    'e621': {'s': 'safe', 'q': 'questionable', 'e': 'explicit'},
}
TRANSIENT_ATTEMPTS = 3
_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None


def _flag(value) -> int | None:
    if value is None:
        return None
    return 1 if str(value).lower() in {'1', 'true'} else 0


def post_row(post: RemotePost, artist_id: int) -> tuple:
    raw = post.raw_metadata or {}
    relationships = raw.get('relationships') or {}
    if post.provider == 'e621':
        parent, children = relationships.get('parent_id'), relationships.get('has_children')
    else:
        parent, children = post.parent_id, raw.get('has_children')
    rating = raw.get('rating') or post.rating
    rating = RATINGS.get(post.provider, {}).get(rating, rating)
    fav = raw.get('fav_count')
    tags = post.tags or {}
    values = {
        'site': post.provider, 'remote_id': str(post.remote_id), 'artist_id': artist_id, 'md5': post.md5,
        'width': post.width or 0, 'height': post.height or 0, 'ext': (post.format or '').lower(), 'rating': rating,
        'score': post.score, 'fav_count': int(fav) if fav not in (None, '') else None,
        'parent_id': str(parent) if parent not in (None, '', 0, '0') else None, 'has_children': _flag(children),
        'created_at': post.created_at, 'file_url': post.image_url or None, 'preview_url': post.preview_url,
        **{column: ' '.join(tags.get(category, [])) for column, category in (
            ('artists', 'artist'), ('characters', 'character'), ('copyrights', 'copyright'),
            ('species', 'species'), ('general', 'general'), ('meta', 'meta'))},
    }
    return tuple(values[column] for column in POST_COLUMNS)


def _transient(exc: Exception) -> bool:
    import httpx
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    response = getattr(exc, 'response', None)
    return response is not None and (response.status_code == 429 or response.status_code >= 500)


async def _search(provider, tag: str, cursor: str | None):
    for attempt in range(TRANSIENT_ATTEMPTS):
        try:
            return await provider.search(tag, cursor, BOORU_SITES[provider.site]['max_limit'], sort='latest')
        except Exception as exc:
            if attempt + 1 == TRANSIENT_ATTEMPTS or not _transient(exc):
                raise
            await asyncio.sleep(2 ** attempt)


def create_job(sites: list[str], max_posts_per_artist: int, refresh: bool) -> dict:
    sites = [site for site in SITES if site in sites]
    if not sites:
        raise ValueError('Choose at least one site')
    if _task and not _task.done():
        raise RuntimeError('A harvest is already running')
    conn = connect()
    try:
        marks = ','.join('?' * len(sites))
        if refresh:
            conn.execute(f"UPDATE artist SET harvest_status='pending', harvest_cursor=NULL, harvested_posts=0 WHERE enabled=1 AND site IN ({marks})", sites)
        else:
            # Errors are retried; finished artists are kept.
            conn.execute(f"UPDATE artist SET harvest_status='pending' WHERE enabled=1 AND harvest_status='error' AND site IN ({marks})", sites)
        totals = {r['site']: {'total': r['total'], 'done': r['done'], 'errors': 0, 'posts': 0, 'current': None}
                  for r in conn.execute(f"""SELECT site, count(*) total, sum(harvest_status='done') done FROM artist
                                            WHERE enabled=1 AND site IN ({marks}) GROUP BY site""", sites)}
        parameters = {'sites': sites, 'max_posts_per_artist': max_posts_per_artist, 'refresh': refresh}
        job_id = conn.execute("INSERT INTO harvest_job (status, parameters, progress, created_at) VALUES ('queued', ?, ?, ?)",
                              (json.dumps(parameters), json.dumps({'sites': totals, 'log': []}), now())).lastrowid
        conn.commit()
        return harvest_row(conn.execute('SELECT * FROM harvest_job WHERE id=?', (job_id,)).fetchone())
    finally:
        conn.close()


class _Progress:
    def __init__(self, job_id: int):
        self.job_id = job_id
        conn = connect()
        try:
            self.data = json.loads(conn.execute('SELECT progress FROM harvest_job WHERE id=?', (job_id,)).fetchone()[0])
        finally:
            conn.close()

    def log(self, message: str) -> None:
        self.data['log'] = (self.data.get('log', []) + [f'{now()[:19]} {message}'])[-200:]

    def save(self, conn, status: str | None = None, error: str | None = None) -> None:
        if status:
            finished = now() if status in {'completed', 'canceled', 'failed'} else None
            conn.execute('UPDATE harvest_job SET status=?, progress=?, error=COALESCE(?, error), finished_at=COALESCE(?, finished_at) WHERE id=?',
                         (status, json.dumps(self.data), error, finished, self.job_id))
        else:
            conn.execute('UPDATE harvest_job SET progress=? WHERE id=?', (json.dumps(self.data), self.job_id))


async def _harvest_site(site: str, progress: _Progress, max_posts: int, stop: asyncio.Event,
                        factory: Callable) -> None:
    state = progress.data['sites'][site]
    try:
        provider = factory(site)
    except ProviderAuthenticationError as exc:
        progress.log(f'{site}: {exc}')
        state['blocked'] = str(exc)
        return
    provider.literal_query = True
    try:
        while not stop.is_set():
            conn = connect()
            try:
                artist = conn.execute("SELECT * FROM artist WHERE enabled=1 AND site=? AND harvest_status='pending' ORDER BY id LIMIT 1",
                                      (site,)).fetchone()
                if not artist:
                    return
                conn.execute("UPDATE artist SET harvest_status='running' WHERE id=?", (artist['id'],))
                state['current'] = artist['display_name']
                progress.save(conn)
                conn.commit()
            finally:
                conn.close()
            cursor, count = artist['harvest_cursor'], artist['harvested_posts']
            try:
                while count < max_posts and not stop.is_set():
                    posts, next_cursor = await _search(provider, artist['tag'], cursor)
                    posts = posts[:max_posts - count]
                    conn = connect()
                    try:
                        conn.executemany(f"INSERT OR REPLACE INTO post ({','.join(POST_COLUMNS)}) VALUES ({','.join('?' * len(POST_COLUMNS))})",
                                         [post_row(post, artist['id']) for post in posts])
                        count += len(posts)
                        state['posts'] += len(posts)
                        cursor = next_cursor
                        conn.execute('UPDATE artist SET harvest_cursor=?, harvested_posts=? WHERE id=?', (cursor, count, artist['id']))
                        conn.commit()
                    finally:
                        conn.close()
                    if not next_cursor:
                        break
                finished = not stop.is_set()
                conn = connect()
                try:
                    if finished:
                        conn.execute("UPDATE artist SET harvest_status='done', harvest_cursor=NULL, harvested_at=?, harvest_error=NULL WHERE id=?",
                                     (now(), artist['id']))
                        state['done'] += 1
                    else:
                        conn.execute("UPDATE artist SET harvest_status='pending' WHERE id=?", (artist['id'],))
                    progress.save(conn)
                    conn.commit()
                finally:
                    conn.close()
            except ProviderAuthenticationError as exc:
                conn = connect()
                try:
                    conn.execute("UPDATE artist SET harvest_status='pending' WHERE id=?", (artist['id'],))
                    conn.commit()
                finally:
                    conn.close()
                progress.log(f'{site}: {exc}')
                state['blocked'] = str(exc)
                return
            except Exception as exc:
                state['errors'] += 1
                progress.log(f"{site}: {artist['display_name']} failed: {exc}")
                conn = connect()
                try:
                    conn.execute("UPDATE artist SET harvest_status='error', harvest_error=? WHERE id=?", (str(exc)[:500], artist['id']))
                    progress.save(conn)
                    conn.commit()
                finally:
                    conn.close()
    finally:
        state['current'] = None
        await provider.close()


async def run_job(job_id: int, stop: asyncio.Event, factory: Callable = create_provider) -> None:
    progress = _Progress(job_id)
    conn = connect()
    try:
        parameters = json.loads(conn.execute('SELECT parameters FROM harvest_job WHERE id=?', (job_id,)).fetchone()[0])
        progress.log('Harvest started')
        progress.save(conn, 'running')
        conn.commit()
    finally:
        conn.close()
    status, error = 'completed', None
    try:
        await asyncio.gather(*(_harvest_site(site, progress, parameters['max_posts_per_artist'], stop, factory)
                               for site in parameters['sites']))
        if stop.is_set():
            status = 'canceled'
    except Exception as exc:
        status, error = 'failed', str(exc)
        progress.log(f'Harvest failed: {exc}')
    progress.log(f'Harvest {status}')
    conn = connect()
    try:
        progress.save(conn, status, error)
        conn.commit()
    finally:
        conn.close()


def launch(job_id: int) -> None:
    global _task, _stop
    _stop = asyncio.Event()
    _task = asyncio.get_running_loop().create_task(run_job(job_id, _stop))


def cancel() -> bool:
    if _task and not _task.done() and _stop:
        _stop.set()
        conn = connect()
        try:
            conn.execute("UPDATE harvest_job SET status='cancelling' WHERE status='running'")
            conn.commit()
        finally:
            conn.close()
        return True
    return False


async def shutdown() -> None:
    if _task and not _task.done() and _stop:
        _stop.set()
        await asyncio.gather(_task, return_exceptions=True)
