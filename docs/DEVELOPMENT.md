# Development

## Layout

```text
backend/app/          FastAPI routes, providers, services, SQLite schema
backend/tests/        Offline unit/integration regressions
backend/benchmarks/   Synthetic benchmarks and browser fixture server
frontend/src/         React UI
frontend/scripts/     Layout/cache tests and isolated Edge workflows
tools/                Backup/restore and optional release builder
docs/                 User and developer documentation
studio.py             Combined launcher and maintenance CLI
Jupyter Studio.ipynb  Remote setup/launch notebook
IMPLEMENTATION_GUIDE.md  Phase history and handoff
```

`library/` is runtime-only and ignored. `.venv`, `node_modules`, `frontend/dist`, logs and release archives are generated locally.

## Setup

Use Python 3.13 and Node.js 22 for the tested development workflow. From the repository root run `python studio.py setup`. This installs `backend/requirements-lock.txt`, runs `npm ci`, and builds the frontend. `backend/requirements.txt` records allowed dependency ranges; the lock records the tested environment. Dependency updates should refresh the lock deliberately and rerun checks.

For live frontend edits use two terminals:

```powershell
# Repository root, Windows
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

```bash
# Repository root, Linux/macOS
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm run dev
```

The Vite development server proxies `/api` and `/static` to the local backend. Use a disposable library for development; migrations and job recovery are real startup behavior.

## Tests

```powershell
# Windows, start at repository root
cd backend
..\.venv\Scripts\python.exe -m unittest discover -s tests
cd ..
```

```bash
# Linux/macOS, start at repository root
cd backend
../.venv/bin/python -m unittest discover -s tests
cd ..
```

```bash
cd frontend
npm test
npm run build
```

Build before the packaged-app subprocess tests. They create temporary libraries and launch isolated local backends. The optional Edge workflows require Windows, Node 22+, installed Edge (or `EDGE_PATH`), and free fixture/debugging ports:

```bash
node scripts/phase7-smoke.mjs --counts --groups --ui --collections --preferences --logs
node scripts/remote-smoke.mjs
```

Browser fixtures use no live providers and retain temporary diagnostics/screenshots. The remote fixture checks URL rewriting and XSRF forwarding; it does not replace testing JupyterHub's authentication.

## Configuration

Environment variables are read by the process; `.env` files are not automatically loaded.

| Variable/flag | Purpose |
| --- | --- |
| `ARTIST_LIBRARY_PATH`, `--library` | Backend-owned persistent library root |
| `ARTIST_DB_PATH`, `--database` | Optional database location; keep on local server storage |
| `ARTIST_PROVIDER_SETTINGS_PATH` | Override provider settings JSON location |
| `ARTIST_LOG_PATH` | Override diagnostics directory |
| `ARTIST_CORS_ORIGINS` | Explicit comma-separated UI origins for a separate frontend |
| `VITE_BACKEND_URL` | Frontend build-time backend URL override; leave unset for the combined app/Jupyter |
| `ARTIST_SERVE_UI=1` | Serve built frontend; the combined launcher sets this |
| `--base-path` | External prefix for a standard prefix-stripping authenticated proxy |

Legacy `ARTIST_*` names and internal `collection`/`folder` aliases remain for compatibility. The launcher and legacy development entrypoints bind to localhost. Authentication is supplied by JupyterHub/private tunneling for remote deployment; the backend is not a multi-user public service.

See [Contributing](../CONTRIBUTING.md) and [Implementation guide](../IMPLEMENTATION_GUIDE.md) before changing storage, query semantics or recovery behavior.
