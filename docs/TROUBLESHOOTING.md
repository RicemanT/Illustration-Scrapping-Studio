# Troubleshooting

| Symptom | What to check |
| --- | --- |
| Processing/server settings return 404 | Restart the backend after updating code, refresh the page, and retry. A new UI with an older backend can expose controls whose endpoints do not exist yet. |
| Port already in use | Stop the known previous app or choose `studio.py run --port 8010`. The launcher does not kill unrelated processes. |
| Library/database already in use | Stop the other instance before launch, backup or restore. Do not delete lock files to bypass an active process. |
| Missing dependencies or UI | Run `python studio.py setup`; use `python studio.py doctor` to check. Python 3.12+ is required; Python 3.13 is the tested local runtime. |
| No Node on Jupyter | Build the frontend on your laptop and upload all of `frontend/dist`, then use `setup --skip-ui-build`; alternatively install Node in your server environment. Re-upload after UI changes. |
| Updated code but old UI | Run setup again. With `--skip-ui-build`, you must upload a new build yourself. Then restart and refresh. |
| Proxy 404/403 | Confirm the Jupyter server-proxy extension is enabled and you are logged in. Installed package presence alone is insufficient. Use the notebook-generated standard proxy URL. |
| Proxy 502/503 | Check `library/launcher.log`, the app process and chosen port. Do not expose the unauthenticated port through a public anonymous tunnel. |
| App returns login HTML | Reopen Jupyter, sign in, then reopen the app link and retry. |
| Provider 401/403 | Test saved credentials and account access. Logs state possible causes without claiming an unproven reason. |
| Provider 429 | Respect provider rate limits; additional workers do not remove throttling. |
| Fewer new images than requested | Inspect job logs for existing sources, quality rejections, unavailable media and errors. Count is successful images, not requested posts. |
| Storage reserve reached | Free space or adjust the reserve in Settings; rerun failed work. Also check account/container quotas. |
| Disk full, permission denied or SQLite locked | Follow the diagnostic's path and evidence. Check both the library and temporary volumes; ensure only one backend owns the database. |
| Missing thumbnails or stale captions | Run Dataset QA and explicitly repair supported selected issues. |
| Browser closes during a job | Reopen the same backend; the browser does not own the work. A server shutdown is different and stops processes. |

For a bug report include the app revision, OS/Python version, local or Jupyter mode, the action attempted, expected/actual result and relevant request/job ID. Share redacted diagnostic excerpts rather than the database, provider settings, cookies or full private library. Logs can still contain local paths and collection names.

Clean-machine Linux installation and your actual JupyterHub configuration require host-side verification. Passing offline fixtures does not establish current live-provider availability.
