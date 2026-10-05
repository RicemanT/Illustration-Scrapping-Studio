"""Standard-library helpers for the public Jupyter launcher."""
from pathlib import Path, PurePosixPath
import hashlib
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

# GitHub Actions publishes the built UI for every frontend version on main as
# assets of this release, named by the Git tree hash of `frontend/`.
UI_RELEASE = 'ui-latest'
UI_MARKER = '.ui-version'


def stop_app(process, grace=60):
    """Stop only the owned process; never force-kill unfinished writes."""
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('Shutdown is still in progress. Check launcher.log and wait, then retry. No update was performed.') from exc


def app_processes(app, proc_root=Path('/proc')):
    """Find this checkout's Linux launcher, regardless of argument order/port."""
    launcher = str(Path(app).resolve() / 'studio.py')
    found = []
    if not proc_root.is_dir():
        return found
    for directory in proc_root.iterdir():
        if not directory.name.isdigit() or int(directory.name) == os.getpid():
            continue
        try:
            args = (directory / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            cwd = (directory / 'cwd').resolve()
            if 'run' in args and any(str((cwd / arg).resolve()) == launcher for arg in args if arg.endswith('studio.py')):
                found.append(int(directory.name))
        except (OSError, ValueError):
            continue
    return found


def assert_stopped(app, port):
    pids = app_processes(app)
    if pids:
        raise RuntimeError(f'This checkout is still running (PID {pids}). Stop those app processes before updating or starting. Use the Diagnose cell; no files were changed.')
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as exc:
            raise RuntimeError(f'Port {port} is occupied. Run Diagnose and stop the identified app, or choose another port. No files were changed.') from exc


def refresh_ui(app, zip_path):
    """Validate/extract first, then replace on the same filesystem with rollback."""
    frontend = Path(app).resolve() / 'frontend'
    dist = frontend / 'dist'
    if frontend.is_symlink() or dist.is_symlink() or any(getattr(p, 'is_junction', lambda: False)() for p in (frontend, dist)):
        raise RuntimeError('Frontend directories must not be symlinks or junctions')
    if not Path(zip_path).is_file():
        raise RuntimeError(f'Upload frontend-ui.zip first: {zip_path}')
    frontend.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ui-update-', dir=frontend) as temporary:
        stage = Path(temporary)
        payload = stage / 'payload'
        payload.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            entries = []
            seen = set()
            for info in archive.infolist():
                # Windows ZIP writers sometimes use backslashes as separators.
                name = info.filename.replace('\\', '/')
                parts = PurePosixPath(name).parts
                mode = (info.external_attr >> 16) & 0o170000
                if not parts or name.startswith('/') or any(p == '..' or ':' in p for p in parts) or mode not in (0, 0o100000, 0o040000):
                    raise ValueError(f'Unsafe UI archive entry: {info.filename!r}')
                if '__MACOSX' in parts or parts[-1] == '.DS_Store':
                    continue
                key = '/'.join(parts).casefold()
                if key in seen:
                    raise ValueError(f'Duplicate UI archive path: {info.filename!r}')
                seen.add(key)
                entries.append((info, parts, name.endswith('/') or mode == 0o040000))
            for info, parts, directory in entries:
                target = payload.joinpath(*parts)
                if directory:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open('wb') as output:
                        shutil.copyfileobj(source, output)
        candidates = list(payload.rglob('index.html'))
        if len(candidates) != 1:
            raise ValueError('UI zip must contain exactly one built index.html with its assets (at the root, in dist/, or inside a wrapper folder).')
        index = candidates[0]
        built = index.parent
        assets = re.findall(r'(?:src|href)=[\"\']([^\"\']+)', index.read_text(encoding='utf-8'))
        for asset in assets:
            if asset.startswith(('http:', 'https:', 'data:', '//', '#')):
                continue
            relative = asset.split('?', 1)[0].split('#', 1)[0].lstrip('/')
            target = (built / relative).resolve()
            if built.resolve() not in target.parents or not target.is_file():
                raise ValueError(f'UI zip is missing a referenced asset: {asset}')
        previous = stage / 'previous'
        if dist.exists():
            dist.rename(previous)
        try:
            built.rename(dist)
        except BaseException:
            if previous.exists():
                previous.rename(dist)
            raise
    return dist


def _git(app, *args):
    return subprocess.run(['git', '-C', str(app), *args], capture_output=True, text=True, check=True).stdout.strip()


def github_repository(app):
    """(owner, name) of the checkout's GitHub origin."""
    url = _git(app, 'remote', 'get-url', 'origin')
    match = re.search(r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$', url)
    if not match:
        raise RuntimeError(f'The origin remote is not a GitHub repository ({url}); install a UI zip with refresh_ui instead.')
    return match.group(1), match.group(2)


def _download(url, opener, timeout=120):
    request = urllib.request.Request(url, headers={'User-Agent': 'IllustrationScrappingStudio-notebook'})
    with opener(request, timeout=timeout) as response:
        return response.read()


def fetch_ui(app, wait=600, poll=20, opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic, log=print):
    """Install the UI that GitHub built for this checkout's exact frontend source.

    Does nothing when the served UI already matches. Right after a push the
    build may still be running; this waits up to `wait` seconds for it.
    """
    app = Path(app).resolve()
    dist = app / 'frontend' / 'dist'
    version = _git(app, 'rev-parse', 'HEAD:frontend')
    marker = dist / UI_MARKER
    if (dist / 'index.html').is_file() and marker.is_file() and marker.read_text(encoding='utf-8').strip() == version:
        log(f'UI is already the build for this version ({version[:12]}).')
        return dist
    owner, name = github_repository(app)
    url = f'https://github.com/{owner}/{name}/releases/download/{UI_RELEASE}/frontend-ui-{version}.zip'
    deadline = clock() + wait
    waiting = False
    while True:
        try:
            data = _download(url, opener)
            expected = _download(url + '.sha256', opener).decode('ascii').split()[0]
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError(f'Downloading the UI failed: HTTP {exc.code} for {url}') from exc
            if clock() >= deadline:
                raise RuntimeError(f'GitHub has no UI build for this version yet ({version[:12]}). Check the "UI build" workflow '
                                   f'on GitHub, then rerun this cell. Nothing was changed.') from exc
            if not waiting:
                log('GitHub is still building the UI for this version; waiting for it (checks every '
                    f'{poll} s, up to {wait // 60} min)...')
                waiting = True
            sleep(poll)
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f'Could not reach GitHub to download the UI ({exc}). Check internet access, or set '
                               'DOWNLOAD_UI = False and install an uploaded UI_ZIP. Nothing was changed.') from exc
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('The downloaded UI does not match its checksum; rerun this cell. Nothing was changed.')
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / 'frontend-ui.zip'
        archive.write_bytes(data)
        refresh_ui(app, archive)
    marker.write_text(f'{version}\n', encoding='utf-8')
    log(f'Installed the UI build for this version ({version[:12]}).')
    return dist
