"""Durable storage for Phase 5 sync jobs and scheduling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_connection
from app.services.diagnostics import job_event


ACTIVE_STATUSES = {"queued", "running", "cancelling"}


def _decode(value: str | None, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def row_to_job(row) -> dict[str, Any]:
    job = dict(row)
    job["parameters"] = _decode(job.get("parameters"), {})
    job["progress"] = _decode(job.get("progress"), {})
    job["logs"] = _decode(job.get("logs"), [])
    job["result"] = _decode(job.get("result"), None)
    job["cancel_requested"] = bool(job.get("cancel_requested"))
    job["job_id"] = job.pop("id")
    job["folder_id"] = job.pop("collection_id", None)
    return job


def _table(table: str) -> str:
    if table not in {"sync_job", "dataset_job"}:
        raise ValueError("Unknown job table")
    return table


def create_job(job: dict[str, Any], table: str = "sync_job") -> dict[str, Any]:
    conn = get_connection()
    conn.execute(
        f"""INSERT INTO {_table(table)}
           (id, kind, trigger, collection_id, provider, status, parameters,
            progress, logs, result, error, cancel_requested, created_at,
            started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["job_id"], job["kind"], job.get("trigger", "manual"),
            job.get("folder_id"), job.get("provider"), job["status"],
            json.dumps(job.get("parameters", {})), json.dumps(job.get("progress", {})),
            json.dumps(job.get("logs", [])), json.dumps(job.get("result")) if job.get("result") is not None else None,
            job.get("error"), 1 if job.get("cancel_requested") else 0,
            job["created_at"], job.get("started_at"), job.get("finished_at"),
        ),
    )
    conn.commit()
    conn.close()
    job_event(job, table.removesuffix("_job"), created=True)
    return job


def save_job(job: dict[str, Any], table: str = "sync_job") -> None:
    conn = get_connection()
    conn.execute(
        f"""UPDATE {_table(table)} SET status = ?, progress = ?, logs = ?, result = ?,
           error = ?, cancel_requested = ?, started_at = ?, finished_at = ?, trigger = ?
           WHERE id = ?""",
        (
            job["status"], json.dumps(job.get("progress", {})), json.dumps(job.get("logs", [])),
            json.dumps(job.get("result")) if job.get("result") is not None else None,
            job.get("error"), 1 if job.get("cancel_requested") else 0,
            job.get("started_at"), job.get("finished_at"), job.get("trigger", "manual"),
            job["job_id"],
        ),
    )
    conn.commit()
    conn.close()
    job_event(job, table.removesuffix("_job"))


def get_job(job_id: str, table: str = "sync_job") -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute(f"SELECT * FROM {_table(table)} WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return row_to_job(row) if row else None


def list_jobs(limit: int = 50, group_id: int | None = None) -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sync_job WHERE (? IS NULL OR json_extract(parameters, '$.group_id') = ?) ORDER BY created_at DESC LIMIT ?",
        (group_id, group_id, min(max(limit, 1), 200))
    ).fetchall()
    conn.close()
    return [row_to_job(row) for row in rows]


def unfinished_jobs() -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM sync_job WHERE status IN ('queued', 'running', 'cancelling') ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [row_to_job(row) for row in rows]


def get_schedule() -> dict[str, Any]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM sync_schedule WHERE id = 1").fetchone()
    conn.close()
    schedule = dict(row)
    schedule["enabled"] = bool(schedule["enabled"])
    return schedule


def update_schedule(enabled: bool, interval_minutes: int, limit_per_source: int, sort: str) -> dict[str, Any]:
    sort = "latest"
    now = datetime.now(timezone.utc)
    next_run = (now + timedelta(minutes=interval_minutes)).isoformat() if enabled else None
    conn = get_connection()
    conn.execute(
        """UPDATE sync_schedule SET enabled = ?, interval_minutes = ?,
           limit_per_source = ?, sort = ?, next_run_at = ?, updated_at = ? WHERE id = 1""",
        (1 if enabled else 0, interval_minutes, limit_per_source, sort, next_run, now.isoformat()),
    )
    conn.commit()
    conn.close()
    return get_schedule()


def advance_schedule() -> dict[str, Any]:
    schedule = get_schedule()
    now = datetime.now(timezone.utc)
    next_run = now + timedelta(minutes=int(schedule["interval_minutes"]))
    conn = get_connection()
    conn.execute(
        "UPDATE sync_schedule SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = 1",
        (now.isoformat(), next_run.isoformat(), now.isoformat()),
    )
    conn.commit()
    conn.close()
    return get_schedule()
