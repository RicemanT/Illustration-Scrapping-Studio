import time
import uuid
import re
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from app.services import diagnostics as log


def install(app):
    def failure(exc, status, detail):
        diagnostic = log.diagnose(exc, status)
        event = log.emit('request.rejected', diagnostic['summary'], 'ERROR' if status >= 500 else 'WARNING', error=exc, diagnostic=diagnostic, status=status)
        return JSONResponse({'detail': log.redact(detail), 'diagnostic': diagnostic, 'request_id': log.context.get().get('request_id'), 'event_id': event['event_id']}, status_code=status)

    @app.exception_handler(HTTPException)
    async def rejected(request, exc):
        response = failure(exc, exc.status_code, exc.detail)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # Pydantic errors contain raw input. Only retain field locations/types.
        details = [{'loc': e['loc'], 'type': e['type'], 'msg': 'Invalid value'} for e in exc.errors()]
        return failure('Input validation failed: ' + str(details), 422, details)

    @app.middleware('http')
    async def trace(request, call_next):
        request_id = uuid.uuid4().hex
        from starlette._utils import get_route_path
        path = get_route_path(request.scope)
        fields = {'request_id': request_id, 'method': request.method, 'path': path}
        match = re.search(r'/(?:folders|collections)/(\d+)', path)
        if match: fields['folder_id'] = int(match.group(1))
        for key in ('provider', 'folder_id', 'job_id'):
            value = request.query_params.get(key)
            if value and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value): fields[key] = value
        token = log.context.set(fields)
        started = time.monotonic()
        quiet = path.startswith('/api/diagnostics') or not path.startswith('/api/')
        level = 'DEBUG' if request.method == 'GET' or path.endswith('/query') else 'INFO'
        if not quiet:
            log.emit('request.started', f'{request.method} {path} started', level)
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                response = failure(exc, 500, 'The operation failed. Open diagnostics using the request ID.')
            if not quiet or response.status_code >= 400:
                log.emit('request.finished', f'{request.method} {path}: HTTP {response.status_code}', 'ERROR' if response.status_code >= 500 else 'WARNING' if response.status_code >= 400 else level, status=response.status_code, duration_ms=round((time.monotonic()-started)*1000))
            response.headers['X-Request-ID'] = request_id
            return response
        finally:
            log.context.reset(token)
