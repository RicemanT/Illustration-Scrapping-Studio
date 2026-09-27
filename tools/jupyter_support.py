"""Standard-library helpers for the public Jupyter launcher."""
from pathlib import Path, PurePosixPath
import os
import re
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
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                parts = PurePosixPath(info.filename).parts
                if not parts or parts[0] != 'dist' or any(p in ('.', '..') or ':' in p for p in parts) or '\\' in info.filename or ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError('UI zip must contain only regular files under dist/')
            archive.extractall(stage)
        built = stage / 'dist'
        index = built / 'index.html'
        if not index.is_file():
            raise ValueError('UI zip must contain dist/index.html')
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
