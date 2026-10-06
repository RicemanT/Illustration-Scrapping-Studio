"""Automatic quality tags from harvested score percentiles.

Raw scores are not comparable: newer posts reach more users and explicit posts
collect more votes. So, as many dataset builders do, a post is ranked only
against posts from the same site, year and rating: it gets masterpiece in the
top X% of that bucket, best quality in the top Y%, and optionally low quality
in the bottom Z%. Buckets with too few posts fall back to the site's whole
year, then to the whole site.

The population is every harvested post in the planner database, kept as
per-bucket score histograms so thresholds and assignments never rescan the
post table. Auto marks only replace marks that were not set by hand.
"""
from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, model_validator

import app.db as db
from app.services.planner_store import connect, now
from app.services.post_dates import YEAR_SQL, post_year

METRICS = {'score': 'score', 'favorites': 'fav_count'}
RATING_ORDER = ('general', 'safe', 'sensitive', 'questionable', 'explicit')
ALL = '*'


class QualityConfig(BaseModel):
    model_config = {'extra': 'forbid'}
    metric: Literal['score', 'favorites'] = 'score'
    masterpiece_top: float = Field(5.0, ge=0, le=100, description='Top percent of a bucket tagged masterpiece')
    best_quality_top: float = Field(10.0, ge=0, le=100, description='Top percent tagged best quality (includes the masterpiece share)')
    low_quality_bottom: float = Field(0.0, ge=0, le=100, description='Bottom percent tagged low quality (0 = never)')
    min_bucket: int = Field(300, ge=1, le=10_000_000, description='Smaller year/rating buckets fall back to a wider one')
    by_year: bool = True
    by_rating: bool = True
    # Aesthetic marks from the image-analysis scorer ensemble (dataset-wide percentile).
    aesthetic_tags: bool = True
    very_aesthetic_top: float = Field(5.0, ge=0, le=100, description='Top percent of analysed images marked very aesthetic')
    aesthetic_top: float = Field(15.0, ge=0, le=100, description='Top percent marked aesthetic (includes the very aesthetic share)')

    @model_validator(mode='after')
    def shares(self):
        if self.aesthetic_top < self.very_aesthetic_top:
            raise ValueError('Aesthetic must cover at least the very aesthetic share')
        if self.best_quality_top < self.masterpiece_top:
            raise ValueError('Best quality must cover at least the masterpiece share')
        if self.best_quality_top + self.low_quality_bottom > 100:
            raise ValueError('The top and bottom shares overlap')
        return self


def _setting(conn, key: str):
    row = conn.execute('SELECT value FROM planner_setting WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else None


def _save_setting(conn, key: str, value) -> None:
    conn.execute('INSERT INTO planner_setting (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, json.dumps(value)))


def get_config() -> QualityConfig:
    conn = connect()
    try:
        saved = _setting(conn, 'quality_config')
    finally:
        conn.close()
    try:
        return QualityConfig.model_validate(saved or {})
    except ValueError:
        return QualityConfig()


def save_config(config: QualityConfig) -> QualityConfig:
    conn = connect()
    try:
        _save_setting(conn, 'quality_config', config.model_dump())
        conn.commit()
    finally:
        conn.close()
    return config


def stats_info(metric: str) -> Optional[dict]:
    conn = connect()
    try:
        return _setting(conn, f'quality_stats:{metric}')
    finally:
        conn.close()


def build_stats(metric: str, progress: Callable[..., None] = lambda **_: None) -> dict:
    """Count harvested posts per (site, year, rating, value). One pass over the post table."""
    column = METRICS[metric]
    conn = connect()
    try:
        estimate = conn.execute('SELECT COALESCE(sum(harvested_posts), 0) FROM artist').fetchone()[0]
        progress(read=0, total=estimate)
        counts: Counter = Counter()
        cursor = conn.execute(f"SELECT site, {YEAR_SQL}, COALESCE(rating, ''), {column} FROM post WHERE {column} IS NOT NULL")
        read = 0
        while rows := cursor.fetchmany(50_000):
            counts.update(rows)
            read += len(rows)
            progress(read=read, total=max(estimate, read))
        conn.execute('DELETE FROM score_hist WHERE metric=?', (metric,))
        conn.executemany('INSERT INTO score_hist (metric, site, year, rating, value, n) VALUES (?, ?, ?, ?, ?, ?)',
                         [(metric, site, year, rating, int(value), n) for (site, year, rating, value), n in counts.items()])
        info = {'built_at': now(), 'posts': read}
        _save_setting(conn, f'quality_stats:{metric}', info)
        conn.commit()
        return info
    finally:
        conn.close()


class Bucket:
    """Sorted values with prefix counts: share of posts at or above / at or below a value."""

    def __init__(self, counter: Counter):
        self.values = sorted(counter)
        self.prefix = [0]
        for value in self.values:
            self.prefix.append(self.prefix[-1] + counter[value])
        self.n = self.prefix[-1]

    def top_share(self, value: int) -> float:
        return 100.0 * (self.n - self.prefix[bisect_left(self.values, value)]) / self.n

    def bottom_share(self, value: int) -> float:
        return 100.0 * self.prefix[bisect_right(self.values, value)] / self.n

    def top_threshold(self, percent: float) -> Optional[int]:
        """Lowest value whose top share is within `percent` (posts at or above it qualify)."""
        for index, value in enumerate(self.values):
            if 100.0 * (self.n - self.prefix[index]) / self.n <= percent:
                return value
        return None

    def bottom_threshold(self, percent: float) -> Optional[int]:
        """Highest value whose bottom share is within `percent`."""
        best = None
        for index, value in enumerate(self.values):
            if 100.0 * self.prefix[index + 1] / self.n <= percent:
                best = value
            else:
                break
        return best


class Histograms:
    def __init__(self, config: QualityConfig, rows):
        self.config = config
        merged: dict[tuple, Counter] = defaultdict(Counter)
        for site, year, rating, value, n in rows:
            year = year if config.by_year else ALL
            rating = rating if config.by_rating else ALL
            merged[(site, year, rating)][value] += n
            if rating != ALL:
                merged[(site, year, ALL)][value] += n
            if year != ALL:
                merged[(site, ALL, ALL)][value] += n
        self.buckets = {key: Bucket(counter) for key, counter in merged.items()}

    def bucket_for(self, site: str, year: int, rating: str):
        config = self.config
        keys = [(site, year if config.by_year else ALL, rating if config.by_rating else ALL),
                (site, year if config.by_year else ALL, ALL), (site, ALL, ALL)]
        for key in dict.fromkeys(keys):
            bucket = self.buckets.get(key)
            if bucket and bucket.n >= config.min_bucket:
                return key, bucket
        return None, None

    def assess(self, site: str, value, year: int, rating: str) -> tuple[Optional[str], dict]:
        if value is None:
            return None, {'reason': 'no score'}
        key, bucket = self.bucket_for(site, year, rating)
        if bucket is None:
            return None, {'reason': f'fewer than {self.config.min_bucket} {site} posts to compare with'}
        top, bottom = bucket.top_share(int(value)), bucket.bottom_share(int(value))
        config = self.config
        tag = ('masterpiece' if top <= config.masterpiece_top else 'best quality' if top <= config.best_quality_top
               else 'low quality' if config.low_quality_bottom and bottom <= config.low_quality_bottom else None)
        return tag, {'metric': config.metric, 'value': int(value), 'top': round(top, 2), 'bottom': round(bottom, 2),
                     'site': site, 'year': None if key[1] == ALL else key[1], 'rating': None if key[2] == ALL else key[2],
                     'n': bucket.n}


def load_histograms(config: QualityConfig) -> Histograms:
    conn = connect()
    try:
        rows = conn.execute('SELECT site, year, rating, value, n FROM score_hist WHERE metric=?', (config.metric,)).fetchall()
    finally:
        conn.close()
    return Histograms(config, [tuple(r) for r in rows])


def thresholds(config: QualityConfig) -> dict:
    """Cut-off values per site, year and rating, like a percentile table."""
    hist = load_histograms(config)

    def cell(key):
        bucket = hist.buckets.get(key)
        if not bucket:
            return None
        return {'n': bucket.n, 'enough': bucket.n >= config.min_bucket,
                'masterpiece': bucket.top_threshold(config.masterpiece_top) if config.masterpiece_top else None,
                'best_quality': bucket.top_threshold(config.best_quality_top) if config.best_quality_top else None,
                'low_quality': bucket.bottom_threshold(config.low_quality_bottom) if config.low_quality_bottom else None}

    sites = []
    for site in sorted({key[0] for key in hist.buckets}):
        years = sorted({key[1] for key in hist.buckets if key[0] == site and key[1] != ALL}, reverse=True)
        ratings = sorted({key[2] for key in hist.buckets if key[0] == site and key[2] != ALL},
                         key=lambda r: (RATING_ORDER.index(r) if r in RATING_ORDER else len(RATING_ORDER), r))
        sites.append({'site': site, 'overall': cell((site, ALL, ALL)), 'ratings': ratings,
                      'years': [{'year': year, 'all': cell((site, year, ALL)),
                                 'ratings': {rating: cell((site, year, rating)) for rating in ratings}} for year in years]})
    return {'config': config.model_dump(), 'stats': stats_info(config.metric), 'sites': sites}


def _folder_artists(conn, folder_ids: Optional[list[int]]):
    rows = conn.execute('SELECT DISTINCT d.folder_id, d.artist_id, a.completed_at FROM delivery_item d JOIN artist a ON a.id=d.artist_id').fetchall()
    wanted = set(folder_ids) if folder_ids is not None else None
    result = {}
    for r in rows:
        if wanted is None or r['folder_id'] in wanted:
            result.setdefault(r['folder_id'], (r['artist_id'], r['completed_at']))
    return result


def apply(folder_ids: Optional[list[int]], config: QualityConfig, progress: Callable[..., None] = lambda **_: None) -> dict:
    """Assign auto quality marks in planner collections; hand-set marks and accepted folders are kept."""
    hist = load_histograms(config)
    column = METRICS[config.metric]
    conn = connect()
    try:
        folders = _folder_artists(conn, folder_ids)
    finally:
        conn.close()
    result = {'folders': 0, 'images': 0, 'assigned': Counter(), 'normal': 0, 'kept_manual': 0, 'no_score': 0,
              'locked_folders': 0, 'missing_folders': 0, 'aesthetic': Counter(), 'aesthetic_kept_manual': 0, 'not_analysed': 0}
    aesthetics = _AestheticTags(config) if config.aesthetic_tags else None
    progress(done=0, total=len(folders))
    main = db.get_connection()
    planner = connect()
    try:
        for index, (folder_id, (artist_id, completed_at)) in enumerate(sorted(folders.items()), 1):
            progress(done=index - 1, total=len(folders))
            if completed_at:
                result['locked_folders'] += 1
                continue
            if not main.execute('SELECT 1 FROM collection WHERE id=?', (folder_id,)).fetchone():
                result['missing_folders'] += 1
                continue
            posts = {(r['site'], r['remote_id']): r for r in planner.execute(
                f'SELECT site, remote_id, {column} AS value, rating, created_at FROM post WHERE artist_id=?', (artist_id,))}
            images, aesthetic_sources = {}, {}
            for r in main.execute("""SELECT i.id, i.quality_source, i.aesthetic_source, s.provider, s.remote_id FROM image i
                                     JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=? ORDER BY s.id""", (folder_id,)):
                post = posts.get((r['provider'], str(r['remote_id']).split(':', 1)[0]))
                aesthetic_sources[r['id']] = r['aesthetic_source']
                if r['id'] not in images or (post is not None and images[r['id']][1] is None):
                    images[r['id']] = (r['quality_source'], post)
            updates = []
            for image_id, (source, post) in images.items():
                if post is None:
                    tag, info = None, {'reason': 'no harvested post'}
                else:
                    tag, info = hist.assess(post['site'], post['value'], post_year(post['created_at']), post['rating'] or '')
                if 'reason' in info:
                    result['no_score'] += 1
                if source == 'manual':
                    result['kept_manual'] += 1
                elif tag:
                    result['assigned'][tag] += 1
                else:
                    result['normal'] += 1
                updates.append((tag, json.dumps(info), tag, image_id))
            main.executemany("""UPDATE image SET quality_auto=?, quality_auto_info=?,
                                  quality_mark=CASE WHEN COALESCE(quality_source, 'auto')='auto' THEN ? ELSE quality_mark END,
                                  quality_source=CASE WHEN COALESCE(quality_source, 'auto')='auto' THEN 'auto' ELSE quality_source END
                                WHERE id=?""", updates)
            if aesthetics is not None:
                aesthetic_updates = []
                for image_id, (_, post) in images.items():
                    tag, info = aesthetics.assess(post)
                    if info is None:
                        result['not_analysed'] += 1
                        continue
                    if aesthetic_sources.get(image_id) == 'manual':
                        result['aesthetic_kept_manual'] += 1
                    elif tag:
                        result['aesthetic'][tag] += 1
                    aesthetic_updates.append((tag, json.dumps(info), tag, image_id))
                main.executemany("""UPDATE image SET aesthetic_auto=?, aesthetic_auto_info=?,
                                      aesthetic_mark=CASE WHEN COALESCE(aesthetic_source, 'auto')='auto' THEN ? ELSE aesthetic_mark END,
                                      aesthetic_source=CASE WHEN COALESCE(aesthetic_source, 'auto')='auto' THEN 'auto' ELSE aesthetic_source END
                                    WHERE id=?""", aesthetic_updates)
            main.commit()
            result['folders'] += 1
            result['images'] += len(updates)
        progress(done=len(folders), total=len(folders))
    finally:
        main.close()
        planner.close()
    if aesthetics is not None:
        aesthetics.close()
    result['assigned'] = dict(result['assigned'])
    result['aesthetic'] = dict(result['aesthetic'])
    return result


class _AestheticTags:
    """very aesthetic / aesthetic from the analysis scorer ensemble's dataset-wide percentile."""

    def __init__(self, config: QualityConfig):
        self.config = config
        self.conn = None
        try:
            from app.analysis.assess import AestheticScale
            from app.analysis.store import connect as analysis_connect
            self.conn = analysis_connect()
            self.scale = AestheticScale.load(self.conn)
        except Exception:
            self.scale = None

    def assess(self, post):
        if post is None or self.scale is None or not self.scale.sorted:
            return None, None
        from app.analysis.store import load_posts
        row = load_posts(self.conn, [(post['site'], str(post['remote_id']))]).get((post['site'], str(post['remote_id'])))
        if row is None:
            return None, None
        ensemble = self.scale.ensemble(row)
        if ensemble is None:
            return None, None
        top = round(100 * (1 - ensemble), 2)
        tag = 'very aesthetic' if top <= self.config.very_aesthetic_top else 'aesthetic' if top <= self.config.aesthetic_top else None
        return tag, {'ensemble': round(ensemble, 4), 'top': top, 'scorers': {k: round(v, 4) for k, v in self.scale.parts(row).items()}}

    def close(self):
        if self.conn is not None:
            self.conn.close()
