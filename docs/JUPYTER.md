# Remote Jupyter setup

Phase 10 runs the scraping/review app on a persistent Jupyter server. Training, GPU configuration and model jobs are outside this app's scope. Local mode remains available.

## Install

1. Clone/upload the source project to the server. Use the directory containing `studio.py` as the app directory. Keep the persistent library separate, for example `/home/jovyan/illustration-library`.
2. Open [Jupyter Studio.ipynb](../Jupyter%20Studio.ipynb) and edit `APP` to that app directory. Python 3.12+ is required. Python 3.13 is the tested local runtime; verify dependency installation on your host.
3. If Node/npm is available, setup installs dependencies and builds the UI. If it is absent, setup downloads the UI that GitHub built for the checkout's exact frontend source (see below) and installs it into the served `frontend/dist` before running `setup --skip-ui-build`, including on a fresh checkout. On a server without internet access, set `DOWNLOAD_UI = False` in the setup cell, run `npm ci` and `npm run build` in `frontend` on your laptop, zip the resulting `frontend/dist` directory as `frontend-ui.zip` (a `dist/` prefix, ZIP-root contents, or a wrapper folder are accepted), upload it to the server and point `UI_ZIP` at it.
4. Keep your own copy of the notebook outside the app folder (File → Save Notebook As…). Edits to the copy inside the checkout block `git pull` whenever the notebook changes upstream; the setup cell explains how to recover if that happens.
4. Run configuration, setup and start cells. Follow the generated link in your authenticated Jupyter session. Do not run the stop cell until you want to stop the app. Rerun setup after every update. It gracefully stops the app owned by the current kernel, then checks for surviving processes and an occupied port before changing files. A shutdown timeout aborts the update; it never force-kills unfinished writes. Git checkouts use `git pull --ff-only origin main`; source archives skip Git. Set `UPDATE_SOURCE=False` to skip pulling source.

## Updates refresh the built UI

`frontend/dist` is git-ignored, so `git pull` updates the source but never the compiled UI that the backend serves. The **UI build** GitHub workflow builds the UI whenever `frontend/` changes on `main` and publishes it as `frontend-ui-<tree>.zip` (plus a `.sha256`) on the `ui-latest` release, where `<tree>` is `git rev-parse HEAD:frontend`. On a server without Node the setup cell pulls, then `fetch_ui` downloads the build for the pulled tree, verifies its checksum and installs it; right after a push it waits up to 10 minutes for the build to appear. It records the installed tree in `frontend/dist/.ui-version` and skips the download when it already matches. The UI is never built from a different frontend source than the one checked out. The notebook extracts into a temporary directory alongside the served UI, validates `dist/index.html` and its referenced local assets, and replaces the old build. Invalid archives leave the previous UI intact. The replacement removes old hashed assets. Use a build from the same source revision you are installing. The setup cell prints the asset filenames it installed; confirm the new `assets/index-*.js` appears there before assuming an update failed. The start cell reports the tail of `launcher.log` when the app exits, and the diagnose cell reports whether the port is still held by an earlier process.

An example JupyterHub path is `/user/YOUR-USERNAME/proxy/8000/`. The notebook derives it from `JUPYTERHUB_SERVICE_PREFIX` instead of hardcoding your username. The backend listens only on 127.0.0.1. The standard proxy removes the external prefix before forwarding; `--base-path` restores correct UI assets, API calls, redirects, images and navigation. Do not use the proxy's `absolute` variant.

Jupyter Server Proxy provides authenticated access to local applications: [official documentation](https://jupyter-server-proxy.readthedocs.io/en/latest/). Installed package presence does not prove the server extension is enabled. If the proxy link gives 404/403, ask the operator to confirm it is enabled for your Jupyter server. If it gives 502/503, inspect `launcher.log` and the running backend. Do not replace it with a public anonymous tunnel. A private SSH port-forward to localhost is an alternative; omit `--base-path` when using a direct localhost tunnel URL.

## Ownership, credentials and reconnecting

All imports, processing, image files, SQLite, sidecars, exports and provider credentials belong to the selected backend. The browser transfers UI requests, previews, uploaded lists/cookies and requested images, not a duplicate library through a laptop backend. Open Settings to inspect the active server and library path. Enter provider settings through the authenticated page. Upload cookies from your own account where needed; browser profiles on the laptop are not server-side browser profiles.

Closing the browser leaves the notebook-launched process running. Reopen the same proxy URL to reconnect to persisted jobs. Server/container restarts still stop the process. Existing sync recovery applies on restart; import/QA recovery retains their existing behavior. This does not add automatic server boot services or guarantee continuation after provider/server shutdown. After a notebook kernel restart, an older app process may remain; the notebook deliberately does not kill unknown processes. Port checks and library/database locks prevent concurrent copies.

## Storage and compute

Settings reports available logical CPUs, Python/platform, library path and actual filesystem capacity/free space. The free-space reserve defaults to 5 GiB and is configurable. Sync/import checks before new media downloads and ingest checks before processing/writing. Below the reserve, items fail with `storage_reserve` diagnostics. Free space or adjust the reserve, then start a new job for failed items.

This is a soft admission guard, not a hard quota or reservation: concurrent/in-flight downloads, frame extraction, exports, other users and OS accounting can cross the threshold. Account/container quotas may differ from filesystem free space. Worker count remains any positive integer; provider throttles remain in force. No GPU is needed. Reported logical CPUs are capability information, not an instruction to immediately use that many concurrent large decodes. Settings preserves your chosen count.

Confirm which directory is persistent with your server operator. Keep SQLite and its library on that server's local persistent volume; do not mount the live database over a network filesystem. Capacity is measured at runtime, including after storage expansion.

## Backups and updates

Stop the app gracefully, then run `studio.py backup` from the server's app environment to an archive outside the library. Backups include credentials and must stay private. Restore into a new directory; see [Installation](INSTALLATION.md). Back up before pulling updated source. Run setup again (or upload a fresh frontend build and use `--skip-ui-build`), then restart. Startup migration snapshots protect metadata; they do not replace full library backups.

No remote server login or deployment was performed by the coding agent. Local subprocess integration tests exercise prefixed UI/API routing; final Jupyter authentication/proxy behavior, dependency installation and scraping must be verified on the actual host.

For complete provider authentication instructions, follow [Provider setup](PROVIDER_SETUP.md), including Windows/Jupyter commands and cookie upload.

## Stop, restart, and recover after a kernel restart

For an ordinary restart, set `STOP_APP=True` in the Stop cell and run it, then run Start once. Repeated stops are harmless. If shutdown takes longer than 60 seconds, inspect `LIBRARY/launcher.log`, allow pending work to finish, and retry; setup will not proceed while the owned process is still stopping.

After a kernel restart, rerun Configuration and Diagnose. Diagnose lists matching app PIDs even if the launcher used a different port. Inspect the printed PID with `ps -p PID -o pid,args`; after confirming it is your app, use `kill -TERM PID` and wait for shutdown. Diagnose never kills anything itself. An occupied port with no matching app requires inspecting the listener, using the printed `ss` command. Do not terminate an unrelated service. Rerun Diagnose, then Setup/update and Start once.

A notebook already open in Jupyter does not automatically adopt cells changed by Git. Close it without saving stale cells and reopen the updated notebook, or obtain a fresh copy; retain your `APP`, `LIBRARY`, `PORT`, and `UI_ZIP` values. The setup cell reloads the helper module after pulling, so new helper functions are available in the same run. Always keep the same persistent library path unless intentionally creating a new library. Run Configuration after refreshing notebook code so it loads the matching helper module.

If an older Setup cell rejects the UI ZIP before reaching `git pull`, pull the latest source in a terminal first, then rerun Configuration to reload the helper before retrying Setup. The archive must contain one built `index.html` and all its referenced assets; Windows ZIP separators are supported.

## Updating for DeviantArt

Finish active jobs and stop Studio before updating. Run the notebook Configuration, Setup/update, and Start cells: setup pulls the latest source and, without Node.js, downloads the matching UI. Refresh the browser after restarting. Keep your existing persistent library path.

In the **server app?s Settings ? DeviantArt**, select **Artwork page / published images**, or **Artwork page; allow thumbnail fallback** if you accept smaller fallback images. Upload your signed-in DeviantArt Netscape cookie export using **Upload DeviantArt cookies**. A file saved in your laptop app is not automatically available to the server. OAuth and cookies are separate; cookies authenticate artwork pages, while OAuth authenticates API access. See [Provider setup](PROVIDER_SETUP.md) for mature browsing settings, weekly original-download limits, and private credential handling.
