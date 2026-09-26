# Remote Jupyter setup

Phase 10 runs the scraping/review app on a persistent Jupyter server. Training, GPU configuration and model jobs are outside this app's scope. Local mode remains available.

## Install

1. Clone/upload the source project to the server. Use the directory containing `studio.py` as the app directory. Keep the persistent library separate, for example `/home/jovyan/illustration-library`.
2. Open [Jupyter Studio.ipynb](../Jupyter%20Studio.ipynb) and edit `APP` to that app directory. Python 3.12+ is required. Python 3.13 is the tested local runtime; verify dependency installation on your host.
3. If Node/npm is available, setup installs dependencies and builds the UI. If it is absent, run `npm ci` and `npm run build` in `frontend` on your laptop, then upload the complete `frontend/dist` directory into the server checkout. The notebook uses `setup --skip-ui-build` in this case. Rebuild and upload again after frontend updates; do not reuse a stale build. No release ZIP is required.
4. Run configuration, setup and start cells. Follow the generated link in your authenticated Jupyter session. Do not run the stop cell until you want to stop the app.

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
