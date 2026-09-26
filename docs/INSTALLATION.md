# Illustration Scrapping Studio: running, backups and updates

## Install from GitHub (primary workflow)

Clone or download the repository and open the project directory containing `studio.py`. Install Python **3.12+** and Node.js **22** with npm (tested version), then run **Setup Studio.bat** followed by **Start Studio.bat**. Setup installs the pinned backend dependencies and builds the frontend. On other platforms, use `python3 studio.py setup` and `python3 studio.py run`.

The repository contains source code, tests, dependency lockfiles, documentation and launchers. A release ZIP is an optional convenience for users who want the already-built UI; it is not required to publish or use the GitHub repository.

The default `library/`, local environments, dependencies, builds, secrets and `releases/` are excluded by `.gitignore`. Keep custom library locations and private backup archives outside the repository. Before the first commit, inspect `git status --short` and the staged file list; ignore rules do not remove files already tracked by Git. Never commit provider credentials or browser cookies. This setup does not upload anything.

## Start the app

Double-click **Start Studio.bat**. It serves the built UI and API at http://127.0.0.1:8000 and opens your browser after startup completes. Keep its window open. Use Ctrl+C to stop and drain workers. This replaces running two development servers; stop those before launching it. Another process on port 8000 is never terminated automatically.

First installation: install Python **3.12 or newer** (3.13 is the tested runtime), then double-click **Setup Studio.bat**. Setup downloads pinned Python dependencies into this copy's `.venv`. A release ZIP already includes the built frontend, so Node.js is unnecessary. Source checkouts need Node.js/npm; setup always uses `npm ci` and rebuilds the UI, including after updates. Setup does not start scraping or modify the library.

Commands from the app directory:

```powershell
.\.venv\Scripts\python.exe studio.py doctor
.\.venv\Scripts\python.exe studio.py run --port 8010
.\.venv\Scripts\python.exe studio.py run --library "D:\My Image Library"
```

The existing `ARTIST_LIBRARY_PATH`, `ARTIST_DB_PATH`, provider configuration and frontend preference names stay compatible. Branding does not rename your disk directories. Default storage remains this installation's `library` directory. The launcher binds only to localhost. This release is a Python-based portable application, not a standalone executable or Windows installer.

## Back up the library

Stop **all** backend instances first, including older app versions. Current versions hold a process lock during their entire lifetime; maintenance commands refuse an active library/database. Older versions do not have this lock and must be stopped manually.

```powershell
.\.venv\Scripts\python.exe studio.py backup "D:\Backups\collection-2026-09-25.zip"
```

Use `--library` and, if needed, `--database` for another configured location. Choose a new ZIP filename outside the library. Existing archives are never overwritten. The archive contains a verified SQLite snapshot, images, sidecars, thumbnails, groups, tag/review history, deletion recovery, library-local exports/logs, and local provider settings/cookies. It also includes an explicitly configured external provider settings JSON as `provider_settings.json`. **Backups contain private credentials; keep them private.**

Environment variables, browser cookie stores, arbitrary external cookie files and exports outside the library are not copied. Record external configuration separately. The restore uses standard library-relative settings locations; clear obsolete external-path environment overrides when switching libraries. Migration snapshots and scratch data are excluded. Symlinks and junctions are rejected rather than followed. The archive manifest records file sizes and SHA-256 hashes. Backup does not repair missing images: use Dataset QA to inspect collection completeness.

## Restore safely

Restore into a **new directory**, never onto your only existing library:

```powershell
.\.venv\Scripts\python.exe studio.py restore "D:\Backups\collection-2026-09-25.zip" "D:\Restored Collection Library"
.\.venv\Scripts\python.exe studio.py run --library "D:\Restored Collection Library"
```

Restore checks the manifest, file hashes, safe paths, database integrity and foreign keys before publishing the new directory. On failure it removes only its own temporary staging directory. It leaves the source library and archive untouched. Scheduled scraping is disabled and interrupted jobs are marked canceled in the restored database so opening the restore cannot start an unexpected download batch. Resume by starting a new job after inspection. Run Dataset QA before retiring the original library. Existing export records retain their historical absolute paths; old exports are not silently redirected or regenerated.

## Updates and migration recovery

1. Stop the app and create a full backup.
2. For a source checkout, update the code with Git (preserving your local changes), then rerun setup to install dependencies and rebuild the UI. For an optional release ZIP, extract it into a separate application directory and run its setup.
3. Launch with `--library` pointing at your existing library if it is outside the application directory. Do not copy an old `.venv` between installations.
4. Verify collections and Dataset QA. Keep the previous release and backup until satisfied.

Before a changed database schema is initialized, startup saves an integrity-checked SQLite snapshot under `migration-backups` beside the configured database. It marks the schema version only after startup succeeds. A failed snapshot prevents startup migration. These snapshots protect metadata; they are **not full image-library backups**. For full rollback, use the full archive into a new directory with the previous release. Do not simply run an older version on an already-migrated live library.

## Building an optional release ZIP

```powershell
cd frontend
npm ci
npm run build
cd ..
.\.venv\Scripts\python.exe tools/build_release.py "releases\Image-Collection-Studio-1.1.0.zip"
```

Build with `VITE_BACKEND_URL` unset so the release uses its own same-origin backend. The builder uses an explicit source allowlist: backend code, built UI, launch/maintenance tools, locked dependencies and user documentation. It excludes the library, logs, `.venv`, `node_modules`, `.env` files and provider credentials. It writes a SHA-256 file beside the ZIP and refuses an existing output filename. Python dependencies are pinned from the tested environment; first setup needs Internet access. This release has not been tested on a clean Windows VM or as a signed executable.

For authenticated Jupyter deployment and a source checkout without Node, see [JupyterHub](JUPYTER.md). `setup --skip-ui-build` requires an existing frontend build and does not refresh it; upload a fresh build after UI changes.
