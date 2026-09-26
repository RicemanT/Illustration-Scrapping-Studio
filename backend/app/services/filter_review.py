"""Stateless dry-run previews; explicit page selections with durable review undo."""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

from app.db import get_connection
from app.services.filters import LocalQuery, LocalFilter, list_images, query_sql
from app.services.qa import _now

_KEY = secrets.token_bytes(32)


def preview(folder_id, query):
    result = list_images(folder_id, query)
    total = list_images(folder_id, LocalQuery(limit=1))['total']
    payload = {'folder_id': folder_id, 'filters': query.filters.model_dump(), 'expires': time.time()+1800,
               'images': [{'id': i['id'], 'sha256': i['sha256'], 'review_status': i['review_status']} for i in result['items']]}
    raw = json.dumps(payload, sort_keys=True).encode()
    token = base64.urlsafe_b64encode(raw).decode() + '.' + hmac.new(_KEY, raw, hashlib.sha256).hexdigest()
    return dict(result, preview_token=token, matched=result['total'], unmatched=total-result['total'],
                reason='Images match all required local filters and none of the excluded tags. Confirmed review affects only explicitly selected IDs from this preview page.')


def apply(folder_id, token, ids, status):
    try:
        encoded, signature = token.rsplit('.', 1)
        raw = base64.urlsafe_b64decode(encoded)
        if not hmac.compare_digest(signature, hmac.new(_KEY, raw, hashlib.sha256).hexdigest()):
            raise ValueError()
        payload = json.loads(raw)
        if payload['folder_id'] != folder_id or payload['expires'] < time.time():
            raise ValueError()
    except Exception as exc:
        raise ValueError('Preview expired or invalid; preview again') from exc
    snapshots = {i['id']: i for i in payload['images']}
    if not ids or not set(ids) <= snapshots.keys():
        raise ValueError('Select only IDs on the previewed page')
    if status not in {'rejected','archived'}:
        raise ValueError('Review action must be rejected or archived')
    filters = LocalFilter.model_validate(payload['filters'])
    filters.image_ids = list(dict.fromkeys(ids))
    cte, where, params = query_sql(folder_id, filters)
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        matches = {r['id']:dict(r) for r in conn.execute(cte + f'SELECT i.* FROM scope i WHERE {where}',params)}
        if set(matches) != set(ids) or any(matches[i]['sha256'] != snapshots[i]['sha256'] or matches[i]['review_status'] != snapshots[i]['review_status'] for i in ids):
            raise ValueError('Some previewed images changed or no longer match; preview again')
        before = []
        for image_id in dict.fromkeys(ids):
            membership = conn.execute('SELECT review_status,selected,rejection_reason FROM collection_image WHERE collection_id=? AND image_id=?',(folder_id,image_id)).fetchone()
            before.append(dict(id=image_id, status=matches[image_id]['review_status'], membership=dict(membership)))
            conn.execute('UPDATE image SET review_status=? WHERE id=?', (status,image_id))
            conn.execute('UPDATE collection_image SET review_status=?,selected=0,rejection_reason=? WHERE collection_id=? AND image_id=?', (status,'Confirmed local filter preview',folder_id,image_id))
        undo_token = uuid.uuid4().hex
        conn.execute('INSERT INTO filter_review(token,folder_id,payload,created_at,applied_at) VALUES (?,?,?,?,?)',
                     (undo_token,folder_id,json.dumps({'before':before,'status':status,'filters':payload['filters']}),_now(),_now()))
        conn.commit()
        return {'changed':len(before),'undo_token':undo_token}
    finally:
        conn.close()


def undo(folder_id, token):
    conn = get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM filter_review WHERE token=? AND folder_id=? AND undone_at IS NULL',(token,folder_id)).fetchone()
        if not row:
            raise ValueError('Review undo not found or already applied')
        payload = json.loads(row['payload'])
        for item in payload['before']:
            current = conn.execute('SELECT review_status FROM image WHERE id=? AND folder_id=?',(item['id'],folder_id)).fetchone()
            if not current or current[0] != payload['status']:
                raise ValueError('Images changed since this action; undo refused')
        for item in payload['before']:
            conn.execute('UPDATE image SET review_status=? WHERE id=?',(item['status'],item['id']))
            state = item['membership']
            conn.execute('UPDATE collection_image SET review_status=?,selected=?,rejection_reason=? WHERE collection_id=? AND image_id=?',
                         (state['review_status'],state['selected'],state['rejection_reason'],folder_id,item['id']))
        conn.execute('UPDATE filter_review SET undone_at=? WHERE token=?',(_now(),token))
        conn.commit()
        return {'restored':len(payload['before'])}
    finally:
        conn.close()
