"""Illustration Scrapping Studio launcher and offline maintenance commands."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools')]
VENV = ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def paths(args):
    library = Path(args.library or os.getenv('ARTIST_LIBRARY_PATH') or ROOT / 'library').expanduser().resolve()
    database = Path(args.database or os.getenv('ARTIST_DB_PATH') or library / 'index.db').expanduser().resolve()
    return library, database


def doctor():
    problems = []
    if sys.version_info < (3, 12): problems.append('Python 3.12 or newer is required')
    for module in ('fastapi','uvicorn','pydantic','httpx','PIL','imagehash','imageio_ffmpeg','cv2','gallery_dl','curl_cffi','multipart'):
        if importlib.util.find_spec(module) is None: problems.append('Missing dependency: ' + module)
    if not (ROOT / 'frontend/dist/index.html').is_file(): problems.append('Built frontend is missing; run setup with Node.js installed')
    print(json.dumps({'python': sys.version.split()[0], 'executable': sys.executable, 'ready': not problems, 'problems': problems}, indent=2))
    return not problems


def setup(skip_ui_build=False):
    if sys.version_info < (3,12): raise RuntimeError('Install Python 3.12+ first')
    if not VENV.exists(): subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
    requirements = ROOT / 'backend/requirements-lock.txt'
    subprocess.run([str(VENV), '-m', 'pip', 'install', '-r', str(requirements)], check=True)
    # Source checkouts must rebuild after updates even when an older build exists.
    if (ROOT / 'frontend/package.json').is_file() and not skip_ui_build:
        npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if not npm: raise RuntimeError('Node.js/npm is needed to build source checkouts. Release ZIPs include the built UI.')
        subprocess.run([npm, 'ci'], cwd=ROOT / 'frontend', check=True)
        environment = dict(os.environ, VITE_BACKEND_URL='')
        subprocess.run([npm, 'run', 'build'], cwd=ROOT / 'frontend', env=environment, check=True)
    elif not (ROOT / 'frontend/dist/index.html').is_file():
        raise RuntimeError('Frontend source and built UI are both missing. Download a complete source checkout or release.')
    print('Setup complete. Run Start Studio.bat or python studio.py run.')


def run(args):
    if not doctor(): raise RuntimeError('Run Setup Studio.bat, then retry')
    library, database = paths(args)
    import re
    prefix = args.base_path.rstrip('/')
    if prefix and (not re.fullmatch(r'/[A-Za-z0-9_/-]+', prefix) or '//' in prefix):
        raise RuntimeError('Base path must be an absolute URL path with safe segments')
    os.environ.update(ARTIST_LIBRARY_PATH=str(library), ARTIST_DB_PATH=str(database), ARTIST_SERVE_UI='1')
    with socket.socket() as probe:
        try: probe.bind(('127.0.0.1', args.port))
        except OSError as exc: raise RuntimeError(f'Port {args.port} is already in use. Close the other server or choose --port.') from exc
    url = f'http://127.0.0.1:{args.port}'
    if not args.no_browser:
        def open_when_ready():
            for _ in range(120):
                try:
                    with urllib.request.urlopen(url + '/api/health', timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(url); return
                except OSError: pass
                time.sleep(.5)
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f'Illustration Scrapping Studio: {url}\nLibrary: {library}\nClose with Ctrl+C. Keep this window open.')
    import uvicorn
    uvicorn.run('app.main:app', host='127.0.0.1', port=args.port, access_log=False, root_path=prefix)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('setup', help='Install pinned dependencies and rebuild UI in source checkouts').add_argument('--skip-ui-build', action='store_true', help='Use an already uploaded frontend/dist build; fail if absent')
    sub.add_parser('doctor', help='Check local runtime without changing the library')
    start = sub.add_parser('run'); start.add_argument('--port', type=int, default=8000); start.add_argument('--no-browser', action='store_true'); start.add_argument('--base-path', default='', help='External authenticated proxy path, such as /user/name/proxy/8000')
    backup = sub.add_parser('backup', help='Close all app instances first; archive includes private settings'); backup.add_argument('destination')
    restore = sub.add_parser('restore', help='Verify and restore into a NEW directory'); restore.add_argument('archive'); restore.add_argument('destination')
    for command in (start, backup):
        command.add_argument('--library'); command.add_argument('--database')
    args = parser.parse_args()
    # Always use this installation's environment after setup, regardless of shell activation.
    if args.command != 'setup' and VENV.exists() and Path(sys.executable).resolve() != VENV.resolve():
        return subprocess.call([str(VENV), str(Path(__file__).resolve()), *sys.argv[1:]])
    try:
        if args.command == 'setup': setup(args.skip_ui_build)
        elif args.command == 'doctor': return 0 if doctor() else 1
        elif args.command == 'run': run(args)
        elif args.command == 'backup':
            from library_archive import backup as create
            print(json.dumps(create(*paths(args), args.destination), indent=2))
        elif args.command == 'restore':
            from library_archive import restore as recover
            print(json.dumps(recover(args.archive, args.destination), indent=2))
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Could not complete {args.command}: {exc}', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
