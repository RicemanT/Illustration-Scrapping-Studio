"""Bounded, redacted local event history and evidence-based failure explanations."""
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from collections import deque
import errno
import json
import os
import re
import sqlite3
import traceback
import uuid
from urllib.parse import urlsplit, urlunsplit

context = ContextVar('diagnostic_context', default={})
_lock = RLock()
_health = {}
MAX_BYTES = 4 * 1024 * 1024
BACKUPS = 4
SECRET = re.compile(r'authorization|cookie|password|passwd|api[_-]?key|token|secret|signature', re.I)


def clean_text(value):
    text = str(value)
    text = re.sub(r'(?im)^(authorization|cookie|set-cookie)\s*:[^\r\n]*', r'\1: [REDACTED]', text)
    # Credentials from configured environment variables can occur without labels.
    for key, secret in os.environ.items():
        if SECRET.search(key) and len(secret) >= 6:
            text = text.replace(secret, '[REDACTED]')
    def url(match):
        try:
            parsed = urlsplit(match.group(0))
            host = parsed.netloc.rsplit('@', 1)[-1]
            return urlunsplit((parsed.scheme, host, parsed.path, '[REDACTED]' if parsed.query else '', ''))
        except ValueError:
            return '[REDACTED URL]'
    text = re.sub(r'https?://[^\s<>"\']+', url, text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;"\']+', 'Bearer [REDACTED]', text)
    text = re.sub(r'''(?ix)(["']?(?:authorization|cookie|password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|signature)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED JWT]', text)
    return text[:12000]


def redact(value, depth=0):
    if depth > 6:
        return '[nested data omitted]'
    if isinstance(value, dict):
        return {str(key): '[REDACTED]' if SECRET.search(str(key)) else redact(item, depth + 1)
                for key, item in list(value.items())[:80]}
    if isinstance(value, (list, tuple)):
        return [redact(item, depth + 1) for item in value[:80]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return clean_text(value)


def diagnose(error, status=None):
    """Keep the observed failure separate from unproven underlying explanations."""
    import httpx
    evidence = clean_text(error)
    text = evidence.lower()
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    number = getattr(error, 'errno', None)
    code, summary, certainty, causes, steps = (
        'unexpected_error', 'The operation could not finish.', 'unknown',
        ['The available evidence does not establish a cause.'],
        ['Open the correlated request/job events and technical details.', 'Retry once if the operation is safe; include this event ID when reporting the problem.'])
    if 'free-space reserve reached:' in text:
        code, summary, certainty = 'storage_reserve', 'The configured free-space reserve prevents new media writes.', 'confirmed'
        causes, steps = ['Available filesystem space is below the configured reserve.'], ['Free space or adjust the storage reserve in Settings, then start a new job for failed items.']
    elif number == errno.ENOSPC or 'no space left' in text or 'disk is full' in text:
        code, summary, certainty = 'storage_full', 'The storage device has no space available for this write.', 'confirmed'
        causes, steps = ['The library or temporary-file volume is full.'], ['Check free space on the library and temporary-file volumes, then retry.']
    elif isinstance(error, PermissionError) or 'permission denied' in text or 'access is denied' in text:
        code, summary, certainty = 'storage_permission', 'The operating system denied file access.', 'confirmed'
        causes, steps = ['Permissions, a locked file, or security software may be preventing access.'], ['Check the path in the evidence and its permissions.', 'Close applications locking the file; retry without deleting the original files.']
    elif isinstance(error, sqlite3.OperationalError) and ('locked' in text or 'busy' in text):
        code, summary, certainty = 'database_busy', 'SQLite could not obtain the required database lock.', 'confirmed'
        causes, steps = ['Another writer or another backend process may be using the database.'], ['Wait for active work to finish and retry.', 'Check for a second backend using the same library.']
    elif isinstance(error, FileNotFoundError):
        code, summary, certainty = 'file_missing', 'A required file or executable was not found.', 'confirmed'
        causes, steps = ['The file may have moved, been deleted, or an optional tool may be missing.'], ['Check the path in the evidence.', 'Run Dataset QA for missing image files; check decoder/provider setup for missing tools.']
    elif status in (401, 403):
        code, summary, certainty = 'access_denied', f'The server rejected access (HTTP {status}).', 'confirmed'
        causes, steps = ['Credentials may be missing or expired, the resource may be restricted, or the provider may be blocking automated access.'], ['Check provider authentication in Settings.', 'Verify that the account can access the resource normally; do not repeatedly retry a denied request.']
    elif status == 429:
        code, summary, certainty = 'rate_limited', 'The provider reported too many requests (HTTP 429).', 'confirmed'
        causes, steps = ['A provider request quota or rate limit was reached.'], ['Wait for the provider cooldown; keep the existing rate limits.', 'Avoid launching competing clients against the same account.']
    elif isinstance(error, (httpx.TimeoutException, TimeoutError)) or 'timed out' in text:
        code, summary, certainty = 'timeout', 'The operation exceeded its wait time.', 'confirmed'
        causes, steps = ['The provider may be slow, or the connection may be stalled.'], ['Check connectivity and provider availability.', 'Retry later; completed image pairs remain available.']
    elif isinstance(error, httpx.ConnectError):
        code, summary, certainty = 'connection_failed', 'A connection to the provider could not be established.', 'confirmed'
        causes, steps = ['DNS, network connectivity, TLS verification, a proxy, or provider availability may be involved.'], ['Read the underlying connection error below.', 'Check the connection, system clock and proxy configuration; do not disable TLS verification.']
    elif status == 404:
        code, summary, certainty = 'not_found', 'The requested resource was not found (HTTP 404).', 'confirmed'
        causes, steps = ['The ID/path may be outdated, the item may have been removed, or it may not be visible to this account.'], ['Verify the folder/image/provider identity and refresh the view.']
    elif status in (400, 409, 422):
        code, summary, certainty = 'request_rejected', f'The action was rejected (HTTP {status}).', 'confirmed'
        causes, steps = [evidence or 'Input validation or an operation conflict prevented the action.'], ['Correct the input or resolve the conflict described in the evidence before retrying.']
    elif status and status >= 500:
        code, summary, certainty = 'server_error', f'A server returned an error (HTTP {status}).', 'confirmed'
        causes, steps = ['An upstream provider or the local backend may have failed; the correlated events identify which.'], ['Review the preceding event and technical details.', 'Retry later if the provider is unavailable.']
    elif any(word in text for word in ('cannot identify image', 'invalid image', 'decode', 'ffmpeg')):
        code, summary, certainty = 'media_processing', 'The media processing step could not finish.', 'likely'
        causes, steps = ['The downloaded content may be unsupported, incomplete, or a required decoder may be unavailable.'], ['Check the media format and decoder status.', 'Inspect the specific decoder error; a web page may have been returned instead of media.']
    elif any(word in text for word in ('authentication', 'credentials', 'login required', 'cookie')):
        code, summary, certainty = 'provider_setup', 'Provider authentication or setup may be incomplete.', 'likely'
        causes, steps = ['The provider requires working account configuration.'], ['Check this provider in Settings and verify its availability message.']
    return dict(code=code, summary=summary, certainty=certainty, evidence=evidence,
                likely_causes=causes, next_steps=steps)


def log_root():
    from app import db
    return Path(os.getenv('ARTIST_LOG_PATH') or (db.DB_PATH.parent / 'logs')).expanduser().resolve()


def _append(root, name, line):
    path = root / name
    if path.exists() and path.stat().st_size + len(line.encode('utf-8')) > MAX_BYTES:
        oldest = root / f'{name}.{BACKUPS}'
        oldest.unlink(missing_ok=True)
        for index in range(BACKUPS - 1, 0, -1):
            previous = root / f'{name}.{index}'
            if previous.exists():
                previous.replace(root / f'{name}.{index + 1}')
        path.replace(root / f'{name}.1')
    with path.open('a', encoding='utf-8') as stream:
        stream.write(line)


def emit(event, message, level='INFO', *, error=None, diagnostic=None, **fields):
    record = dict(at=datetime.now(timezone.utc).isoformat(), event_id=uuid.uuid4().hex,
                  level=level, event=event, message=message, **context.get())
    record.update(fields)
    if error is not None:
        record['diagnostic'] = diagnose(error)
        record['exception_type'] = type(error).__name__
        if isinstance(error, BaseException):
            record['traceback'] = ''.join(traceback.format_exception(error))[-12000:]
    if diagnostic:
        record['diagnostic'] = diagnostic
    record = redact(record)
    root = log_root()
    try:
        with _lock:
            record['at'] = datetime.now(timezone.utc).isoformat()
            line = json.dumps(record, ensure_ascii=False, default=str) + '\n'
            root.mkdir(parents=True, exist_ok=True)
            _append(root, 'events.jsonl', line)
            if level in ('WARNING', 'ERROR'):
                _append(root, 'errors.jsonl', line)
            _health[str(root)] = {'writable': True, 'last_error': None}
    except (OSError, ValueError) as exc:
        # Diagnostics must not turn a completed library operation into a failure.
        _health[str(root)] = {'writable': False, 'last_error': clean_text(exc)}
    return record


def status():
    root = log_root()
    return {'directory': str(root), 'bytes_per_file': MAX_BYTES, 'backups': BACKUPS,
            'retention': 'Five 4 MiB event files plus five 4 MiB warning/error files; oldest events expire.',
            **_health.get(str(root), {'writable': None, 'last_error': None})}


def read_events(*, level=None, search='', request_id='', job_id='', folder_id=None, provider='', before='', limit=100):
    """Scan a bounded retained history, newest first; expose the scan boundary."""
    root = log_root()
    matches = []
    scanned = 0
    exhausted = True
    name = 'errors.jsonl' if level in ('ERROR', 'WARNING') else 'events.jsonl'
    for index in range(BACKUPS + 1):
        path = root / (name if index == 0 else f'{name}.{index}')
        try:
            with _lock:
                lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            continue
        for line in reversed(lines):
            scanned += 1
            try:
                item = json.loads(line)
            except ValueError:
                continue
            cursor = item.get('at', '') + '|' + item.get('event_id', '')
            if before and cursor >= before:
                continue
            if level == 'ACTIVITY' and item.get('level') == 'DEBUG':
                continue
            if level and level != 'ACTIVITY' and item.get('level') != level:
                continue
            if request_id and item.get('request_id') != request_id:
                continue
            if job_id and str(item.get('job_id', '')) != job_id:
                continue
            if folder_id is not None and str(item.get('folder_id', '')) != str(folder_id):
                continue
            if provider and item.get('provider') != provider:
                continue
            if search and search.casefold() not in line.casefold():
                continue
            matches.append(item)
            if len(matches) > limit:
                exhausted = False
                break
        if not exhausted:
            break
    items = matches[:limit]
    return {'items': items, 'next_cursor': (items[-1]['at'] + '|' + items[-1]['event_id']) if not exhausted and items else None,
            'scanned': scanned, 'retention_exhausted': exhausted, 'storage': status()}


def job_event(job, domain='sync', *, created=False):
    status_value = job.get('status', 'unknown')
    # Save calls may contain high-frequency progress; lifecycle states are distinct.
    signature = (domain, job['job_id'])
    key = (str(log_root()), *signature)
    with _lock:
        if _seen_jobs.get(key) == status_value and not created:
            return
        _seen_jobs[key] = status_value
        if len(_seen_jobs) > 4000:
            del _seen_jobs[next(iter(_seen_jobs))]
    error = job.get('error')
    level = 'ERROR' if status_value == 'failed' else 'WARNING' if (job.get('result') or {}).get('errors', 0) else 'INFO'
    emit(f'{domain}.job.{status_value}', f'{domain.title()} job {status_value}', level,
         error=error, job_id=job['job_id'], folder_id=job.get('folder_id'), provider=job.get('provider') or job.get('parameters', {}).get('provider'),
         processing=job.get('processing') or job.get('parameters', {}).get('processing'), group_id=job.get('parameters', {}).get('group_id'), progress=job.get('progress'), result=job.get('result'), trigger=job.get('trigger'))


_seen_jobs = {}
