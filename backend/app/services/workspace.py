"""App-owned scratch storage; never derive media destinations from cwd or TEMP."""

from pathlib import Path

from app.db import LIBRARY_PATH


def scratch_directory() -> Path:
    path = LIBRARY_PATH / ".scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()
