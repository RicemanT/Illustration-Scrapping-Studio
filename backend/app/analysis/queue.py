"""Which posts get analysed, and where their medium-size samples come from.

Per artist: the newest posts (where the latest style is decided), an even
sample of older posts (for the career style), and every post already
downloaded into the artist's collection (for review flags and calibration).
Posts the metadata filters always reject are skipped.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

from app.analysis.store import AnalysisConfig, connect as analysis_connect
from app.services.planner_select import ADAPTIVE_TAGS, PlannerConfig, rejection
from app.services.planner_store import connect as planner_connect
from app.services.post_dates import sortable_time

STATIC_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'avif'}


def sample_urls(row) -> list[str]:
    """Medium-size image candidates for a post, best first; the original is the fallback."""
    site, md5 = row['site'], (row['md5'] or '').lower()
    ext = (row['ext'] or '').lower()
    original = row['file_url'] if ext in STATIC_EXTS else None
    urls = []
    if site == 'danbooru' and len(md5) == 32:
        urls += [f'https://cdn.donmai.us/sample/{md5[:2]}/{md5[2:4]}/sample-{md5}.jpg',
                 f'https://cdn.donmai.us/720x720/{md5[:2]}/{md5[2:4]}/{md5}.webp']
    elif site == 'e621' and len(md5) == 32:
        urls.append(f'https://static1.e621.net/data/sample/{md5[:2]}/{md5[2:4]}/{md5}.jpg')
    elif site == 'gelbooru' and row['file_url'] and '/images/' in row['file_url'] and len(md5) == 32:
        base = row['file_url'].split('/images/', 1)[0]
        urls.append(f'{base}/samples/{md5[:2]}/{md5[2:4]}/sample_{md5}.jpg')
    if original:
        urls.append(original)
    if row['preview_url'] and not urls:
        urls.append(row['preview_url'])
    return [url for url in dict.fromkeys(urls) if url and url.startswith('https://')]


def _latest_config(planner) -> PlannerConfig:
    row = planner.execute("SELECT config FROM run WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone()
    return PlannerConfig.model_validate(json.loads(row['config'])) if row else PlannerConfig()


def scope_artists(planner, scope: str, artist_ids: Optional[Iterable[int]] = None) -> list[int]:
    if scope == 'artists':
        return sorted({int(a) for a in artist_ids or []})
    if scope == 'planner':
        return [r[0] for r in planner.execute('SELECT DISTINCT artist_id FROM delivery_item ORDER BY artist_id')]
    return [r[0] for r in planner.execute('SELECT id FROM artist WHERE enabled=1 ORDER BY id')]


def choose_posts(rows: list, newest: int, older: int) -> list:
    """Newest posts plus an even spread over the older ones."""
    ordered = sorted(rows, key=lambda r: sortable_time(r['created_at']), reverse=True)
    head, rest = ordered[:newest], ordered[newest:]
    if older and rest:
        step = max(1.0, len(rest) / older)
        picks = sorted({min(len(rest) - 1, int(i * step)) for i in range(min(older, len(rest)))})
        head += [rest[i] for i in picks]
    return head


def build_queue(job_id: int, artist_ids: list[int], config: AnalysisConfig, reanalyze: bool = False) -> dict:
    planner = planner_connect()
    analysis = analysis_connect()
    try:
        plan_config = _latest_config(planner)
        done = set() if reanalyze else {(r['site'], r['remote_id']) for r in analysis.execute(
            "SELECT site, remote_id FROM analysis_post WHERE status='done'")}
        counts = {'artists': 0, 'queued': 0, 'already': 0, 'filtered': 0}
        for artist_id in artist_ids:
            artist = planner.execute('SELECT site FROM artist WHERE id=?', (artist_id,)).fetchone()
            if not artist:
                continue
            family = 'e621' if artist['site'] == 'e621' else 'danbooru'
            blocked = plan_config.tag_set('blocked', family) - ADAPTIVE_TAGS
            rows = planner.execute('SELECT * FROM post WHERE artist_id=?', (artist_id,)).fetchall()
            usable = [r for r in rows if rejection(r, plan_config, blocked) is None]
            counts['filtered'] += len(rows) - len(usable)
            delivered = {(r['site'], r['remote_id']) for r in planner.execute(
                "SELECT site, remote_id FROM delivery_item WHERE artist_id=? AND status IN ('done', 'skipped')", (artist_id,))}
            chosen = choose_posts(usable, config.newest_posts, config.older_posts)
            chosen += [r for r in rows if (r['site'], r['remote_id']) in delivered]
            batch = []
            for row in {(r['site'], r['remote_id']): r for r in chosen}.values():
                key = (row['site'], str(row['remote_id']))
                if key in done:
                    counts['already'] += 1
                    continue
                urls = sample_urls(row)
                if urls:
                    batch.append((job_id, key[0], key[1], artist_id, json.dumps(urls)))
            analysis.executemany('INSERT OR IGNORE INTO analysis_queue (job_id, site, remote_id, artist_id, urls) VALUES (?, ?, ?, ?, ?)', batch)
            counts['queued'] += len(batch)
            counts['artists'] += 1
        analysis.commit()
        return counts
    finally:
        analysis.close()
        planner.close()
