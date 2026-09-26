from app.services.diagnostics import emit, context, job_event
"""Bounded dataset work using the shared durable job store and serializer."""
import asyncio
import json
import threading
import uuid
from collections import Counter
from pathlib import Path

from PIL import Image

from app.db import get_connection
from app.services import sync_jobs
from app.services.filters import LocalFilter, query_sql
from app.services.qa import DatasetQAService, _now, _sha256
from app.services.tags import TagService
from app.services.storage_lock import pair_write

TABLE = 'dataset_job'
_tasks = {}
_stops = {}
_worker = threading.Lock()


def get_job(job_id):
    return sync_jobs.get_job(job_id, TABLE)


def save(job):
    sync_jobs.save_job(job, TABLE)


def create(folder_id, filters, repairs=None):
    conn = get_connection()
    try:
        if not conn.execute('SELECT 1 FROM collection WHERE id=?', (folder_id,)).fetchone():
            raise LookupError('Folder not found')
        if conn.execute("SELECT 1 FROM dataset_job WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone():
            raise ValueError('A dataset job is already active; wait or cancel it first')
    finally:
        conn.close()
    job = dict(job_id=uuid.uuid4().hex, kind='repair' if repairs is not None else 'scan',
               folder_id=folder_id, status='queued', created_at=_now(),
               parameters={'filters': filters.model_dump(), 'repairs': repairs},
               progress={'completed': 0, 'total': 0, 'errors': 0, 'warnings': 0, 'by_code': {}, 'last_id': 0}, logs=[])
    # Snapshot IDs in SQLite; never hold a large selection in Python/browser RAM.
    sync_jobs.create_job(job, TABLE)
    cte, where, params = query_sql(folder_id, filters)
    conn = get_connection()
    try:
        conn.execute(cte + f'INSERT INTO dataset_job_target(job_id,image_id) SELECT ?,i.id FROM scope i WHERE {where}', [params[0], job['job_id'], *params[1:]])
        job['progress']['total'] = conn.execute('SELECT count(*) FROM dataset_job_target WHERE job_id=?', (job['job_id'],)).fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    save(job)
    return job


def issues(job_id, offset=0, limit=100):
    conn = get_connection()
    try:
        total = conn.execute('SELECT count(*) FROM dataset_job_issue WHERE job_id=?', (job_id,)).fetchone()[0]
        items = [dict(json.loads(r['payload']), issue_id=r['id']) for r in conn.execute(
            'SELECT id,payload FROM dataset_job_issue WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?', (job_id, limit, offset))]
        return dict(items=items, total=total, next_cursor=str(offset + len(items)) if offset + len(items) < total else None)
    finally:
        conn.close()


@pair_write
def repair_one(root, folder_id, image_id, action, stop):
    """Decode outside the write lock, then revalidate and atomically replace only derived output."""
    qa = DatasetQAService(root)
    validation = qa.validate_collection(folder_id, [image_id])
    if not validation['image_count']:
        return {'image_id': image_id, 'status': 'skipped', 'message': 'Image was deleted'}
    relevant = [i for i in validation['issues'] if action in i['allowed_actions']]
    if not relevant:
        return {'image_id': image_id, 'status': 'skipped', 'message': 'Repair no longer needed or allowed'}
    if any(i['code'] in {'missing_file','corrupt_image','hash_mismatch','unsafe_path','wrong_folder_owner','path_collision','sidecar_collision'} for i in validation['issues']):
        raise ValueError('Source image is missing, changed, corrupt, or unsafe; repair refused')
    conn = get_connection()
    temp = None
    try:
        row = conn.execute('SELECT * FROM image WHERE id=? AND folder_id=?', (image_id, folder_id)).fetchone()
        if not row:
            return {'image_id': image_id, 'status': 'skipped'}
        source = (Path(root) / 'images' / row['path']).resolve()
        image_root = (Path(root) / 'images').resolve()
        thumb_root = (Path(root) / 'thumbnails').resolve()
        relative = row['thumb_path'] or str(Path(row['path']).with_name(f"{row['sha256']}_thumb.jpg"))
        destination = (thumb_root / relative).resolve() if action == 'thumbnail' else source.with_suffix('.txt').resolve()
        expected_root = thumb_root if action == 'thumbnail' else image_root
        if image_root not in source.parents or expected_root not in destination.parents:
            raise ValueError('Repair path escapes the library')
        # Require the owning folder namespace, not just arbitrary library paths.
        slug = conn.execute('SELECT slug FROM collection WHERE id=?', (folder_id,)).fetchone()[0]
        if (image_root / slug).resolve() not in source.parents or (expected_root / slug).resolve() not in destination.parents:
            raise ValueError('Repair path is outside its owning folder')
        before = _sha256(destination) if destination.is_file() else None
        tags = TagService(root)._effective_tags(conn, image_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.tmp')
        if action == 'thumbnail':
            with Image.open(source) as image:
                image.thumbnail((400,400))
                image.convert('RGB').save(temp, format='JPEG', quality=85)
        elif action == 'sidecar':
            temp.write_text(', '.join(tags), encoding='utf-8')
        else:
            raise ValueError('Unsupported repair')
        if stop.is_set():
            raise InterruptedError('Repair canceled before replacement')
        if _sha256(source) != row['sha256']:
            raise ValueError('Source bytes changed during repair')
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT * FROM image WHERE id=? AND folder_id=?', (image_id, folder_id)).fetchone()
        if not current or current['path'] != row['path'] or current['thumb_path'] != row['thumb_path'] or current['sha256'] != row['sha256']:
            raise ValueError('Image changed or was deleted during repair')
        if action == 'sidecar' and tags != TagService(root)._effective_tags(conn, image_id):
            raise ValueError('Tags changed during repair; run validation again')
        if action == 'thumbnail' and conn.execute("SELECT 1 FROM image WHERE id<>? AND lower(replace(thumb_path,char(92),'/'))=? LIMIT 1", (image_id,relative.replace('\\','/').lower())).fetchone():
            raise ValueError('Thumbnail is shared with another image; repair refused')
        previous = destination.read_bytes() if destination.is_file() else None
        after = _sha256(temp)
        temp.replace(destination)
        try:
            if action == 'thumbnail':
                conn.execute('UPDATE image SET thumb_path=? WHERE id=?', (relative.replace('\\','/'), image_id))
            conn.commit()
        except Exception:
            if previous is None:
                destination.unlink(missing_ok=True)
            else:
                temp.write_bytes(previous)
                temp.replace(destination)
            raise
        return dict(image_id=image_id, status='repaired', action=action, path=str(destination), before_sha256=before, after_sha256=after)
    finally:
        conn.close()
        if temp:
            temp.unlink(missing_ok=True)


def _run(job_id, root, stop):
    with _worker:
        job = get_job(job_id)
        context.set({**context.get(), "job_id": job_id, "folder_id": job["folder_id"]})
        job.update(status='running', started_at=job.get('started_at') or _now(), error=None)
        save(job)
        try:
            qa = DatasetQAService(root)
            while not stop.is_set():
                conn = get_connection()
                targets = conn.execute('SELECT image_id FROM dataset_job_target WHERE job_id=? AND image_id>? ORDER BY image_id LIMIT 25', (job_id, job['progress']['last_id'])).fetchall()
                conn.close()
                if not targets:
                    break
                for target in targets:
                    if stop.is_set():
                        break
                    image_id = target[0]
                    if job['kind'] == 'scan':
                        result = qa.validate_collection(job['folder_id'], [image_id])
                        findings = result['issues']
                    else:
                        findings = []
                        for action in job['parameters']['repairs'].get(str(image_id), []):
                            if stop.is_set():
                                break
                            try:
                                result = repair_one(root, job['folder_id'], image_id, action, stop)
                                findings.append(dict(result, code='repair_result', severity='info', allowed_actions=[]))
                            except InterruptedError:
                                break
                            except Exception as exc:
                                emit("dataset_jobs.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
                                findings.append(dict(image_id=image_id, code='repair_failed', severity='error', message=str(exc), allowed_actions=[]))
                    if stop.is_set():
                        # Atomic repairs already completed remain valid; keep their audit.
                        if job['kind'] == 'scan':
                            break
                    counts = Counter(job['progress']['by_code'])
                    conn = get_connection()
                    try:
                        for finding in findings:
                            emit("dataset.finding", finding.get("message", finding["code"]), finding["severity"].upper(), image_id=image_id, finding=finding)
                            conn.execute('INSERT INTO dataset_job_issue(job_id,image_id,payload) VALUES (?,?,?)', (job_id, image_id, json.dumps(finding)))
                            counts[finding['code']] += 1
                            for severity in ('error', 'warning'):
                                if finding['severity'] == severity:
                                    job['progress'][severity+'s'] += 1
                        job['progress']['by_code'] = dict(counts)
                        if not stop.is_set():
                            job['progress'].update(last_id=image_id, completed=job['progress']['completed']+1)
                        # Findings and checkpoint commit together for restart-safe scans.
                        conn.execute('UPDATE dataset_job SET progress=? WHERE id=?', (json.dumps(job['progress']),job_id))
                        conn.commit()
                    finally:
                        conn.close()
            job['status'] = 'canceled' if stop.is_set() else 'completed'
            job['result'] = dict(job['progress'], ready=job['progress']['errors']==0 and job['progress']['total']>0 and not stop.is_set())
        except Exception as exc:
            emit("dataset_jobs.failed", "Operation failed; see evidence and suggested next steps", "ERROR", error=exc)
            job.update(status='failed', error=str(exc))
        finally:
            job['cancel_requested'] = stop.is_set()
            job['finished_at'] = _now()
            save(job)


def launch(job_id, root):
    stop = threading.Event()
    _stops[job_id] = stop
    async def run():
        try:
            await asyncio.to_thread(_run, job_id, root, stop)
        finally:
            _stops.pop(job_id, None)
            _tasks.pop(job_id, None)
    _tasks[job_id] = asyncio.create_task(run())


def cancel(job_id):
    job = get_job(job_id)
    if not job:
        raise LookupError('Dataset job not found')
    if job_id in _stops:
        _stops[job_id].set()
        conn = get_connection()
        try:
            conn.execute("UPDATE dataset_job SET status='cancelling',cancel_requested=1 WHERE id=? AND status IN ('queued','running')", (job_id,))
            conn.commit()
        finally:
            conn.close()
    return get_job(job_id)


def recover():
    conn = get_connection()
    conn.execute("UPDATE dataset_job SET status='interrupted', error='Backend stopped; resume explicitly to continue remaining images' WHERE status IN ('queued','running','cancelling')")
    conn.commit()
    conn.close()


async def shutdown():
    for stop in list(_stops.values()):
        stop.set()
    if _tasks:
        await asyncio.gather(*list(_tasks.values()), return_exceptions=True)
