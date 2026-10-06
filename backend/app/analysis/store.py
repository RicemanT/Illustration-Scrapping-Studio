"""Analysis results, style vectors, jobs and settings (`<planner>/analysis.db`)."""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

import numpy as np
from pydantic import BaseModel, Field, field_validator

from app.services.planner_store import now, planner_dir

# Numeric results per post. Scorers feed the aesthetic ensemble; the rest are
# content classifiers used by the adaptive gates and review flags.
SCORERS = ('ws3', 'ws4', 'naflex', 'aps25', 'dbaes')
SCORER_LABELS = {'ws3': 'waifu-scorer v3', 'ws4': 'waifu-scorer v4-beta', 'naflex': 'Naflex (SigLIP2)',
                 'aps25': 'aesthetic-predictor v2.5', 'dbaes': 'DeepGHS dbaesthetic'}
CLASSIFIERS = ('polished', 'rough', 'monochrome', 'cls_3d', 'cls_comic', 'cls_illustration', 'cls_bangumi', 'real', 'ai', 'mono')
COLUMNS = SCORERS + ('dbaes_pct',) + CLASSIFIERS + ('era_conf',)
STYLE_MODELS = ('dinov2', 'dinov3')

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
"""


def connect() -> sqlite3.Connection:
    path = planner_dir() / 'analysis.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.executescript(SCHEMA)
    return conn


class AnalysisConfig(BaseModel):
    """How the worker runs. GPU use is capped: only the listed GPUs, busy at most `duty` of the time."""
    model_config = {'extra': 'ignore'}
    gpus: str = Field('0,1', max_length=40, description='CUDA device numbers the worker may use, e.g. "0,1"; empty = CPU')
    duty: float = Field(0.5, ge=0.05, le=1.0, description='Largest share of time each batch keeps the GPUs busy')
    batch_size: int = Field(16, ge=1, le=256)
    newest_posts: int = Field(200, ge=0, le=100000, description="Each artist's newest posts analysed")
    older_posts: int = Field(100, ge=0, le=100000, description='Older posts sampled evenly across the rest of the career')
    download_interval: float = Field(0.25, ge=0.05, le=30, description='Seconds between sample downloads per site')
    download_workers: int = Field(4, ge=1, le=32)
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


def save_results(conn, results: Iterable[dict]) -> int:
    """Store worker results: one dict per post with site, remote_id, artist_id, status, scores and vectors."""
    count = 0
    for r in results:
        values = [r['site'], r['remote_id'], r.get('artist_id'), r['status'], r.get('error'), now(), r.get('source'),
                  ','.join(r.get('models', [])), *[r.get('scores', {}).get(c) for c in COLUMNS], r.get('era'),
                  json.dumps(r['extra']) if r.get('extra') else None]
        conn.execute(f"""INSERT OR REPLACE INTO analysis_post (site, remote_id, artist_id, status, error, analyzed_at, source, models,
                         {', '.join(COLUMNS)}, era, extra) VALUES ({', '.join('?' * len(values))})""", values)
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
                found[(site, r['remote_id'])] = decode_vector(r['vector'])
    return found


def stats() -> dict:
    conn = connect()
    try:
        counts = {r['status']: r['n'] for r in conn.execute('SELECT status, count(*) AS n FROM analysis_post GROUP BY status')}
        models = {r['model']: r['n'] for r in conn.execute('SELECT model, count(*) AS n FROM analysis_vector GROUP BY model')}
        scorers = {name: conn.execute(f'SELECT count(*) FROM analysis_post WHERE {name} IS NOT NULL').fetchone()[0] for name in SCORERS}
        return {'posts': counts, 'vectors': models, 'scorers': scorers}
    finally:
        conn.close()
