"""Audit tracked and unignored publication candidates without printing secret values."""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {'library', '.venv', 'venv', 'node_modules', '__pycache__', '.secrets', 'releases', 'backups', 'dist'}
FORBIDDEN_NAMES = {'provider_settings.json', 'provider_settings.tmp', 'cookies.txt'}
ARTIFACT = re.compile(r'\.(?:db(?:-.+)?|sqlite3?(?:-.+)?|zip|7z|log(?:\..+)?|pyc|png|jpg|jpeg|webp)$', re.I)
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'AWS access key': re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
}

def known_local_secrets():
    values = set()
    path = ROOT / 'library/provider_settings.json'
    if not path.exists(): return values
    def visit(value, key=''):
        if isinstance(value, dict):
            for name, item in value.items(): visit(item, name)
        elif isinstance(value, str) and len(value) >= 12 and re.search(r'token|password|api.?key|secret', key, re.I):
            values.add(value)
    visit(json.loads(path.read_text(encoding='utf-8')))
    return values


def main():
    try:
        result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT, capture_output=True)
    except FileNotFoundError:
        print('Repository audit could not run: install Git and reopen your terminal.', file=sys.stderr)
        return 2
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()
        print('Repository audit could not run. No files were audited.', file=sys.stderr)
        print(detail or f'Git exited with code {result.returncode}.', file=sys.stderr)
        if 'dubious ownership' in detail:
            print('If this is your trusted checkout, run the exact-path safe.directory command shown by Git, then retry. Do not use a wildcard trust exception.', file=sys.stderr)
        return 2
    paths = sorted(set(result.stdout.decode('utf-8').strip('\0').split('\0')) - {''})
    secrets = known_local_secrets()
    failures = []
    for name in paths:
        path = ROOT / name
        parts = set(Path(name).parts)
        if parts & FORBIDDEN_PARTS or path.name in FORBIDDEN_NAMES or (path.name.startswith('.env') and path.name != '.env.example') or ARTIFACT.search(name):
            failures.append((name, 'private/generated artifact'))
            continue
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            failures.append((name, 'symbolic link/junction'))
            continue
        if not path.is_file(): continue
        if path.stat().st_size > 2 * 1024 * 1024:
            failures.append((name, 'unexpected file larger than 2 MiB'))
            continue
        try: text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            failures.append((name, 'unexpected binary/non-UTF-8 file'))
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text): failures.append((name, label))
        if any(value in text for value in secrets): failures.append((name, 'matches a preserved local secret'))
        if path.suffix == '.ipynb':
            for cell in json.loads(text).get('cells', []):
                if cell.get('outputs') or cell.get('execution_count') is not None:
                    failures.append((name, 'notebook contains execution state'))
                    break
    for name, reason in failures: print(f'{name}: {reason}')
    print(f'Audited {len(paths)} publication candidates; {len(failures)} findings. Secret values are never printed.')
    return 1 if failures else 0

if __name__ == '__main__':
    raise SystemExit(main())
