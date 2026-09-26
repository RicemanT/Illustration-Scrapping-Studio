"""Backend-owned capability reporting and free-space admission checks."""
import os
import platform
import shutil
from pathlib import Path
from app.db import LIBRARY_PATH, get_connection


def reserve_gib():
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM app_setting WHERE key='storage_reserve_gib'").fetchone()
        return float(row[0]) if row else 5.0
    finally:
        conn.close()


def capabilities():
    disk = shutil.disk_usage(LIBRARY_PATH)
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
    return dict(platform=platform.system(), python=platform.python_version(), logical_cpus=os.cpu_count(), available_cpus=affinity,
                library_path=str(LIBRARY_PATH), disk_total_bytes=disk.total, disk_free_bytes=disk.free,
                reserve_gib=reserve_gib(), jupyter=bool(os.getenv('JUPYTERHUB_SERVICE_PREFIX')),
                storage_note='Filesystem capacity; account/container quotas may be lower. Active downloads can consume additional space.')


class StorageReserveError(RuntimeError):
    pass


def require_space(path=None):
    path = Path(path or LIBRARY_PATH)
    free = shutil.disk_usage(path).free
    reserve = reserve_gib()
    if free < reserve * 1024**3:
        raise StorageReserveError(f'Free-space reserve reached: {free / 1024**3:.2f} GiB available; {reserve:g} GiB reserved. Free space or adjust the reserve in Settings before retrying. No new media admitted.')
