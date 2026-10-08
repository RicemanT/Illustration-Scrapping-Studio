"""Analysis results, style vectors, jobs and settings (`<planner>/analysis.db`)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from pydantic import BaseModel, Field, field_validator

from app.services.planner_store import now, planner_dir

# Numeric results per post. Scorers feed the aesthetic ensemble; the rest are
# content classifiers used by the adaptive gates and review flags.
SCORERS = ('ws3', 'ws4', 'naflex', 'aps25', 'dbaes', 'anzhc')
SCORER_LABELS = {'ws3': 'waifu-scorer v3', 'ws4': 'waifu-scorer v4-beta', 'naflex': 'Naflex (SigLIP2)',
                 'aps25': 'aesthetic-predictor v2.5', 'dbaes': 'DeepGHS dbaesthetic', 'anzhc': "Anzhc's score (Danbooru percentile)"}
CLASSIFIERS = ('polished', 'rough', 'monochrome', 'cls_3d', 'cls_comic', 'cls_illustration', 'cls_bangumi', 'real', 'ai', 'mono')
COLUMNS = SCORERS + ('dbaes_pct',) + CLASSIFIERS + ('era_conf', 'anzhc_class')
STYLE_MODELS = ('dinov2', 'dinov3')
# Model names as the worker reports them (a post is fully analysed when it has output from every enabled one).
MODEL_FLAGS = ('dinov2', 'dinov3', 'ws3', 'ws4', 'naflex', 'aps25', 'deepghs', 'anzhc')

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS analysis_post (
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    artist_id INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    analyzed_at TEXT NOT NULL,
    source TEXT,
    models TEXT,
    {', '.join(f'{c} REAL' for c in COLUMNS)},
    era TEXT,
    extra TEXT,
    PRIMARY KEY (site, remote_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS analysis_post_artist ON analysis_post(artist_id);
CREATE TABLE IF NOT EXISTS analysis_vector (
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (site, remote_id, model)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS analysis_job (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    params TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    pid INTEGER,
    stop_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS analysis_queue (
    job_id INTEGER NOT NULL,
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    artist_id INTEGER,
    urls TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    PRIMARY KEY (job_id, site, remote_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS analysis_queue_state ON analysis_queue(job_id, state);
CREATE TABLE IF NOT EXISTS analysis_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_sample (
    site TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (site, remote_id)
) WITHOUT ROWID;
"""


def connect() -> sqlite3.Connection:
    path = planner_dir() / 'analysis.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.executescript(SCHEMA)
    present = {r[1] for r in conn.execute('PRAGMA table_info(analysis_post)')}
    for column in COLUMNS:
        if column not in present:  # scorers added after the database was created
            conn.execute(f'ALTER TABLE analysis_post ADD COLUMN {column} REAL')
    return conn


def sample_path(site: str, remote_id: str) -> Path:
    """Where a kept sample image lives (`<planner>/analysis-samples/<site>/<last two digits>/<id>`)."""
    remote_id = str(remote_id)
    return planner_dir() / 'analysis-samples' / site / remote_id[-2:].rjust(2, '0') / remote_id


def enabled_models(config: 'AnalysisConfig') -> list[str]:
    return [name for name in MODEL_FLAGS if getattr(config, name, False)]


class AnalysisConfig(BaseModel):
    """How the worker runs. GPU use is capped: only the listed GPUs, busy at most `duty` of the time."""
    model_config = {'extra': 'ignore'}
    gpus: str = Field('0,1', max_length=40, description='CUDA device numbers the worker may use, e.g. "0,1"; empty = CPU')
    duty: float = Field(0.5, ge=0.05, le=1.0, description='Largest share of time each batch keeps the GPUs busy')
    batch_size: int = Field(16, ge=1, le=256)
    newest_posts: int = Field(200, ge=0, le=100000, description="Each artist's newest posts analysed")
    older_posts: int = Field(100, ge=0, le=100000, description='Older posts sampled evenly across the rest of the career')
    download_interval: float = Field(0.25, ge=0.05, le=30, description='Seconds between sample downloads per site')
    download_workers: int = Field(4, ge=1, le=32, description='Parallel downloads per site')
    max_temp: int = Field(80, ge=0, le=100, description='Pause while a used GPU is at or above this temperature in °C (0 = never)')
    keep_samples: bool = Field(True, description='Keep downloaded samples on disk, so adding a model later needs no new downloads')
    dinov2: bool = True
    dinov3: bool = True
    dinov2_repo: str = 'facebook/dinov2-large'
    dinov3_repo: str = 'facebook/dinov3-vitl16-pretrain-lvd1689m'
    dino_ranges: list[str] = Field(default_factory=lambda: ['4-6', '10-14', '20-24'],
                                   description='Transformer block ranges whose mean-pooled patch tokens are stored')
    style_range: str = Field('4-6', description='Block range the planner uses for style')
    ws3: bool = True
    ws4: bool = True
    naflex: bool = True
    aps25: bool = True
    deepghs: bool = True
    anzhc: bool = True

    @field_validator('dino_ranges')
    @classmethod
    def ranges(cls, value):
        parsed = [parse_range(item) for item in value]
        if not parsed:
            raise ValueError('Store at least one DINO block range')
        return [f'{a}-{b}' for a, b in parsed]

    @field_validator('style_range')
    @classmethod
    def style(cls, value):
        a, b = parse_range(value)
        return f'{a}-{b}'

    def gpu_list(self) -> list[int]:
        return [int(part) for part in self.gpus.replace(' ', '').split(',') if part.strip().isdigit()]


def parse_range(text: str) -> tuple[int, int]:
    parts = str(text).replace(' ', '').split('-')
    try:
        a, b = (int(parts[0]), int(parts[-1]))
    except (ValueError, IndexError) as exc:
        raise ValueError(f'Block range "{text}" must look like 4-6') from exc
    if not 1 <= a <= b <= 64:
        raise ValueError(f'Block range "{text}" must be between 1 and 64')
    return a, b


def get_config(conn: Optional[sqlite3.Connection] = None) -> AnalysisConfig:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute("SELECT value FROM analysis_setting WHERE key='config'").fetchone()
    finally:
        if own:
            conn.close()
    try:
        return AnalysisConfig.model_validate(json.loads(row['value']) if row else {})
    except ValueError:
        return AnalysisConfig()


def save_config(config: AnalysisConfig) -> AnalysisConfig:
    conn = connect()
    try:
        conn.execute("INSERT INTO analysis_setting (key, value) VALUES ('config', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (config.model_dump_json(),))
        conn.commit()
    finally:
        conn.close()
    return config


def vector_key(model: str, block_range: str) -> str:
    return f'{model}:{block_range}'


def encode_vector(vector) -> bytes:
    array = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(array)) or 1.0
    return (array / norm).astype(np.float16).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


def valid_vector(vector: np.ndarray) -> bool:
    return bool(np.isfinite(vector).all()) and float(np.linalg.norm(vector)) > 1e-3


def purge_invalid_vectors(conn) -> int:
    """Delete stored style vectors that are NaN/inf/zero (DINOv3 in float16 overflowed) and forget that their model
    analysed those posts, so the next run redoes just that model (from kept samples when there are some).
    Runs once per database; returns the number of vectors removed."""
    if conn.execute("SELECT 1 FROM analysis_setting WHERE key='vector_check'").fetchone():
        return 0
    bad: dict[tuple, set] = {}
    for row in conn.execute('SELECT site, remote_id, model, vector FROM analysis_vector'):
        if not valid_vector(decode_vector(row['vector'])):
            bad.setdefault((row['site'], row['remote_id']), set()).add(row['model'])
    removed = 0
    for (site, remote_id), keys in bad.items():
        for key in keys:
            conn.execute('DELETE FROM analysis_vector WHERE site=? AND remote_id=? AND model=?', (site, remote_id, key))
            removed += 1
        models = {key.split(':', 1)[0] for key in keys}
        row = conn.execute('SELECT models FROM analysis_post WHERE site=? AND remote_id=?', (site, remote_id)).fetchone()
        if row and row['models']:
            kept = [m for m in row['models'].split(',') if m and m not in models]
            conn.execute('UPDATE analysis_post SET models=? WHERE site=? AND remote_id=?', (','.join(kept), site, remote_id))
    conn.execute("INSERT OR REPLACE INTO analysis_setting (key, value) VALUES ('vector_check', ?)", (json.dumps({'removed': removed, 'at': now()}),))
    conn.commit()
    return removed


def save_results(conn, results: Iterable[dict]) -> int:
    """Store worker results: one dict per post with site, remote_id, artist_id, status, scores and vectors.

    Results merge into what is stored: a run with only new models keeps the earlier scores, and a failed
    re-download never replaces earlier results.
    """
    count = 0
    keep = ', '.join(f'{c}=COALESCE(excluded.{c}, analysis_post.{c})' for c in (*COLUMNS, 'era', 'extra', 'source', 'artist_id'))
    for r in results:
        old = conn.execute('SELECT status, models FROM analysis_post WHERE site=? AND remote_id=?', (r['site'], r['remote_id'])).fetchone()
        if old and old['status'] == 'done' and r['status'] != 'done':
            continue
        earlier = [m for m in (old['models'] or '').split(',') if m] if old and old['status'] == 'done' else []
        values = [r['site'], r['remote_id'], r.get('artist_id'), r['status'], r.get('error'), now(), r.get('source'),
                  ','.join(dict.fromkeys([*earlier, *r.get('models', [])])), *[r.get('scores', {}).get(c) for c in COLUMNS], r.get('era'),
                  json.dumps(r['extra']) if r.get('extra') else None]
        conn.execute(f"""INSERT INTO analysis_post (site, remote_id, artist_id, status, error, analyzed_at, source, models,
                         {', '.join(COLUMNS)}, era, extra) VALUES ({', '.join('?' * len(values))})
                         ON CONFLICT(site, remote_id) DO UPDATE SET status=excluded.status, error=excluded.error,
                         analyzed_at=excluded.analyzed_at, models=excluded.models, {keep}""", values)
        for model, vector in (r.get('vectors') or {}).items():
            conn.execute('INSERT OR REPLACE INTO analysis_vector (site, remote_id, model, vector) VALUES (?, ?, ?, ?)',
                         (r['site'], r['remote_id'], model, encode_vector(vector)))
        count += 1
    return count


def load_posts(conn, keys: list[tuple[str, str]]) -> dict[tuple, dict]:
    """Analysis rows for (site, remote_id) keys."""
    found = {}
    for site in {key[0] for key in keys}:
        ids = [key[1] for key in keys if key[0] == site]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for r in conn.execute(f"SELECT * FROM analysis_post WHERE status='done' AND site=? AND remote_id IN ({','.join('?' * len(chunk))})",
                                  (site, *chunk)):
                found[(r['site'], r['remote_id'])] = dict(r)
    return found


def load_vectors(conn, keys: list[tuple[str, str]], model: str) -> dict[tuple, np.ndarray]:
    found = {}
    for site in {key[0] for key in keys}:
        ids = [key[1] for key in keys if key[0] == site]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for r in conn.execute(f"SELECT remote_id, vector FROM analysis_vector WHERE model=? AND site=? AND remote_id IN ({','.join('?' * len(chunk))})",
                                  (model, site, *chunk)):
                vector = decode_vector(r['vector'])
                if valid_vector(vector):  # never let a broken vector into a style decision
                    found[(site, r['remote_id'])] = vector
    return found


FAILURE_KINDS = (
    ('rate limited', 'Rate limited by the site (HTTP 429/503); retried later'),
    ('HTTP 404', 'File not found on the site (HTTP 404; deleted or moved)'),
    ('HTTP 403', 'Access refused (HTTP 403)'),
    ('HTTP 410', 'File gone (HTTP 410)'),
    ('Timeout', 'Network timeout'),
    ('ConnectError', 'Could not connect'),
    ('Error:', 'Network error'),
    ('not an image', 'Download was not a readable image'),
    ('file too large', 'File larger than 40 MB'),
    ('no URL', 'No sample address (no file or hash in the metadata)'),
)


def failure_kind(error: Optional[str]) -> str:
    """A readable category for a stored failure (the last file tried decides)."""
    last = (error or '').split('; ')[-1]
    if last.strip() in ('HTTP 429', 'HTTP 503'):  # older runs stored the bare status
        last = 'rate limited'
    for needle, label in FAILURE_KINDS:
        if needle in last:
            return label
    if 'HTTP' in last:
        return 'Other HTTP error (' + last.split('HTTP', 1)[1].strip().split()[0] + ')'
    return 'Model or other error'


def failure_report(limit: int = 100) -> dict:
    """Why posts could not be analysed: counts per reason and site, and the most recent examples."""
    conn = connect()
    try:
        rows = conn.execute("SELECT site, remote_id, artist_id, error, analyzed_at, source FROM analysis_post WHERE status='failed' "
                            'ORDER BY analyzed_at DESC').fetchall()
    finally:
        conn.close()
    by_reason: dict[str, dict] = {}
    for row in rows:
        entry = by_reason.setdefault(failure_kind(row['error']), {'total': 0, 'sites': {}})
        entry['total'] += 1
        entry['sites'][row['site']] = entry['sites'].get(row['site'], 0) + 1
    examples = [{**dict(row), 'reason': failure_kind(row['error'])} for row in rows[:limit]]
    return {'failed': len(rows), 'reasons': sorted(({'reason': k, **v} for k, v in by_reason.items()), key=lambda r: -r['total']),
            'examples': examples}


def stats() -> dict:
    conn = connect()
    try:
        counts = {r['status']: r['n'] for r in conn.execute('SELECT status, count(*) AS n FROM analysis_post GROUP BY status')}
        models = {r['model']: r['n'] for r in conn.execute('SELECT model, count(*) AS n FROM analysis_vector GROUP BY model')}
        scorers = {name: conn.execute(f'SELECT count(*) FROM analysis_post WHERE {name} IS NOT NULL').fetchone()[0] for name in SCORERS}
        # Mean and range per scorer, so a scorer stuck at one value shows up while a job runs.
        summary = {}
        for name in SCORERS:
            row = conn.execute(f'SELECT count({name}), avg({name}), min({name}), max({name}) FROM analysis_post').fetchone()
            if row[0]:
                summary[name] = {'count': row[0], 'mean': round(row[1], 3), 'min': round(row[2], 3), 'max': round(row[3], 3)}
        samples = conn.execute('SELECT count(*), COALESCE(sum(bytes), 0) FROM analysis_sample').fetchone()
        return {'posts': counts, 'vectors': models, 'scorers': scorers, 'score_summary': summary,
                'samples': {'count': samples[0], 'bytes': samples[1]}}
    finally:
        conn.close()
