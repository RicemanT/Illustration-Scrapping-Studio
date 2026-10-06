from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os
import uvicorn

from app.db import init_db, LIBRARY_PATH, DB_PATH
from contextlib import asynccontextmanager
from pathlib import Path
from app.services.runtime import LibraryLease, prepare_migration
from app.services.images import ImageService

@asynccontextmanager
async def lifespan(application):
    with LibraryLease(LIBRARY_PATH, DB_PATH):
        marker, version = prepare_migration(DB_PATH)
        await startup_event()
        marker.write_text(version)
        try:
            yield
        finally:
            await shutdown_event()

# Initialize FastAPI app
app = FastAPI(
    title="Illustration Scrapping Studio",
    description="Artist, character and tag collections for image datasets",
    version="1.1.0",
    lifespan=lifespan
)

cors_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "ARTIST_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
    ).split(",")
    if origin.strip()
]

# CORS middleware for a local UI or an explicitly allowed remote UI.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

# Static file serving for images and thumbnails
LIBRARY_PATH.mkdir(parents=True, exist_ok=True)
(LIBRARY_PATH / "images").mkdir(exist_ok=True)
(LIBRARY_PATH / "thumbnails").mkdir(exist_ok=True)

app.mount("/static/images", StaticFiles(directory=str(LIBRARY_PATH / "images")), name="images")
app.mount("/static/thumbnails", StaticFiles(directory=str(LIBRARY_PATH / "thumbnails")), name="thumbnails")


@app.middleware('http')
async def guard_interrupted_folder_moves(request, call_next):
    # If a filesystem error interrupted a move, allow retry but do not start
    # new writers against the old database paths. Startup also completes moves.
    from starlette._utils import get_route_path
    route_path = get_route_path(request.scope)
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and not route_path.endswith('/move') and not route_path.startswith('/api/diagnostics'):
        from app.db import get_connection
        from starlette.responses import JSONResponse
        conn = get_connection()
        try:
            pending = conn.execute('SELECT folder_id FROM folder_move LIMIT 1').fetchone()
        finally:
            conn.close()
        if pending:
            return JSONResponse({'detail': f'Folder {pending[0]} has an interrupted move. Retry its move before other changes.'}, status_code=409)
    return await call_next(request)


async def startup_event():
    """Initialize database on startup."""
    from app.services.diagnostics import emit
    emit("app.starting", "Initializing database and recovering interrupted work")
    try:
        init_db()
        from app.services.settings import configure_worker_pool
        configure_worker_pool()
        from app.services.groups import recover_moves
        recover_moves(LIBRARY_PATH)
        from app.services.dataset_jobs import recover
        recover()
        from app.services import planner_store
        planner_store.recover()
        image_service = ImageService(LIBRARY_PATH)
        migrated = image_service.migrate_to_collection_folders()
        recovered = image_service.reconcile_filesystem()
        image_service.backfill_posted_at()
        # Existing sidecars are inspected by Dataset QA. Rewriting every pair on
        # startup would silently repair stale/missing files without the explicit
        # selection and confirmation required by the repair workflow. Imports and
        # tag/policy edits still derive their own sidecars immediately.
        from app.routes import sync
        resumed_jobs = await sync.start_background_services()
        from app.services.diagnostics import emit
        emit("app.ready", "Backend ready; startup recovery finished", migrated=migrated, recovered=recovered, resumed_jobs=resumed_jobs)
    except Exception as exc:
        from app.services.diagnostics import emit
        emit("app.lifecycle_failed", "Backend lifecycle operation failed", "ERROR", error=exc)
        raise


async def shutdown_event():
    """Drain native workers while leaving interrupted jobs resumable."""
    try:
        from app.routes import sync
        await sync.stop_background_services()
        from app.services.dataset_jobs import shutdown
        await shutdown()
        from app.services import planner_harvest, planner_delivery
        await planner_harvest.shutdown()
        await planner_delivery.shutdown()
        try:
            from app.analysis import service as analysis_service
            import asyncio
            await asyncio.to_thread(analysis_service.shutdown)
        except Exception as exc:  # the analysis database may not exist yet
            from app.services.diagnostics import emit
            emit("service.notice", f"Analysis worker shutdown: {exc}", "WARNING")
        from app.routes.planner import close_thumbnail_client
        await close_thumbnail_client()
        from app.services.diagnostics import emit
        emit("app.stopped", "Backend workers stopped")
    except Exception as exc:
        from app.services.diagnostics import emit
        emit("app.lifecycle_failed", "Backend lifecycle operation failed", "ERROR", error=exc)
        raise


@app.get("/")
async def root():
    return {"message": "Illustration Scrapping Studio API", "version": "1.0.0"}


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "database": "connected",
        "library_path": str(LIBRARY_PATH)
    }


# Import and include routers
from app.routes import collections, images, tags, sync, providers, search, imports, duplicates, exports, settings
from app.routes import dataset
from app.routes import groups
app.include_router(groups.router, prefix="/api/groups", tags=["groups"])
from app.routes import planner
app.include_router(planner.router, prefix="/api/planner", tags=["planner"])
from app.routes import tracker as tracker_routes
app.include_router(tracker_routes.router, prefix="/api/tracker", tags=["tracker"])
from app.routes import analysis as analysis_routes
app.include_router(analysis_routes.router, prefix="/api/analysis", tags=["analysis"])
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])

app.include_router(collections.router, prefix="/api/folders", tags=["folders"])
# Kept as a compatibility alias for existing bookmarks and API clients. New
# frontend code and documentation use /api/folders exclusively.
app.include_router(collections.router, prefix="/api/collections", tags=["legacy collections"], include_in_schema=False)
app.include_router(images.router, prefix="/api/images", tags=["images"])
app.include_router(tags.router, prefix="/api/tags", tags=["tags"])
app.include_router(sync.router, prefix="/api/sync", tags=["sync"])
app.include_router(providers.router, prefix="/api/providers", tags=["providers"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(imports.router, prefix="/api/imports", tags=["imports"])
app.include_router(duplicates.router, prefix="/api/duplicates", tags=["duplicates"])
app.include_router(exports.router, prefix="/api/exports", tags=["exports"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])



from app.routes import diagnostics
from app.services.request_diagnostics import install
app.include_router(diagnostics.router, prefix="/api/diagnostics", tags=["diagnostics"])
install(app)

# Optional production UI; API and static image routes keep precedence.
if os.getenv('ARTIST_SERVE_UI') == '1':
    from starlette.exceptions import HTTPException as StarletteHTTPException
    class SPAFiles(StaticFiles):
        async def get_response(self, path, scope):
            route_path = path.replace(chr(92), '/').lstrip('/')
            if route_path in {'api', 'static'} or route_path.startswith(('api/', 'static/')):
                raise StarletteHTTPException(404)
            if route_path == 'index.html' or not route_path or not Path(route_path).suffix:
                from html import escape
                from starlette.responses import HTMLResponse
                prefix = scope.get('root_path', '').rstrip('/')
                # StaticFiles' mount is '/' so root_path is the externally configured prefix.
                html = (frontend / 'index.html').read_text(encoding='utf-8')
                html = html.replace('<head>', '<head><base href="' + escape(prefix + '/', quote=True) + '"><meta name="studio-base" content="' + escape(prefix, quote=True) + '">', 1)
                return HTMLResponse(html, headers={'Cache-Control': 'no-store'})
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code != 404 or Path(path).suffix:
                    raise
                return await super().get_response('index.html', scope)
    @app.middleware('http')
    async def api_slashes(request, call_next):
        from starlette._utils import get_route_path
        if get_route_path(request.scope) in {'/api/folders', '/api/collections', '/api/exports'}:
            from starlette.responses import RedirectResponse
            return RedirectResponse(request.scope.get('root_path', '').rstrip('/') + get_route_path(request.scope) + '/', status_code=307)
        return await call_next(request)
    frontend = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'
    app.router.routes = [route for route in app.router.routes if getattr(route, 'path', None) != '/']
    app.mount('/', SPAFiles(directory=frontend, html=True), name='ui')

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info"
    )
