from datetime import datetime, timezone

from app.db import get_connection


DEFAULT_PARALLEL_WORKERS = 2
MAX_PARALLEL_WORKERS = None


def get_parallel_workers() -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM app_setting WHERE key = 'parallel_workers'"
    ).fetchone()
    conn.close()
    try:
        value = int(row[0]) if row else DEFAULT_PARALLEL_WORKERS
    except (TypeError, ValueError):
        value = DEFAULT_PARALLEL_WORKERS
    return max(value, 1)


def set_parallel_workers(workers: int) -> int:
    workers = max(int(workers), 1)
    conn = get_connection()
    conn.execute(
        """INSERT INTO app_setting (key, value, updated_at) VALUES ('parallel_workers', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (str(workers), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()
    return workers


from contextvars import ContextVar
import json
from app.models import ProcessingSettings
processing_context = ContextVar('processing_policy', default=None)

def get_processing_settings():
    snapshot = processing_context.get()
    if snapshot is not None: return dict(snapshot)
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM app_setting WHERE key='processing'").fetchone()
        return ProcessingSettings.model_validate(json.loads(row[0]) if row else {}).model_dump()
    finally:
        conn.close()

def set_processing_settings(settings):
    value = ProcessingSettings.model_validate(settings).model_dump()
    conn = get_connection()
    try:
        conn.execute("INSERT INTO app_setting(key,value,updated_at) VALUES('processing',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (json.dumps(value), datetime.now(timezone.utc).isoformat()))
        conn.commit()
    finally: conn.close()
    return value


# Thread workers are allocated lazily; no hidden asyncio default-pool ceiling.
from concurrent.futures import ThreadPoolExecutor
from weakref import WeakKeyDictionary
import asyncio
_pools = WeakKeyDictionary()

def configure_worker_pool(workers=None):
    loop = asyncio.get_running_loop()
    count = workers or get_parallel_workers()
    old = _pools.get(loop)
    if old and old[0] == count: return
    executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix='collection-worker')
    loop.set_default_executor(executor)
    _pools[loop] = (count, executor)
    if old: old[1].shutdown(wait=False)
