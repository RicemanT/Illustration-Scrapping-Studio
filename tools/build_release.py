"""Build a shareable source/runtime release; never include the personal library."""
from pathlib import Path
import hashlib
import zipfile
ROOT = Path(__file__).resolve().parents[1]

def build(destination):
    if not (ROOT / 'frontend/dist/index.html').is_file(): raise RuntimeError('Build frontend first')
    sources = [ROOT / name for name in ('studio.py','Start Studio.bat','Setup Studio.bat','README.md','QUICKSTART.md','CONTRIBUTING.md','LICENSE','IMPLEMENTATION_GUIDE.md','Jupyter Studio.ipynb','backend/requirements.txt','backend/requirements-lock.txt')]
    for directory in ('backend/app', 'tools', 'docs', 'frontend/dist'):
        folder = ROOT / directory
        if folder.is_symlink() or (hasattr(folder, 'is_junction') and folder.is_junction()): raise RuntimeError('Release directories must not be links')
        extensions = ({'.md'} if directory == 'docs' else {'.py'}) if directory != 'frontend/dist' else {'.html','.js','.css','.svg','.png','.ico','.woff','.woff2','.ttf','.webp','.jpg','.jpeg'}
        sources.extend(path for path in folder.rglob('*') if path.is_file() and '__pycache__' not in path.parts and path.suffix in extensions)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for path in sources:
            if path.is_symlink(): raise RuntimeError('Release source must not be a link')
            archive.write(path, 'Illustration Scrapping Studio/' + path.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(digest + '  ' + destination.name + '\n')
    print(str(destination.resolve()), digest)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('destination'); args = parser.parse_args()
    build(args.destination)
