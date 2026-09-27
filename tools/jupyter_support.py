"""Standard-library helpers for the public Jupyter launcher."""
from pathlib import Path, PurePosixPath
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import zipfile


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
