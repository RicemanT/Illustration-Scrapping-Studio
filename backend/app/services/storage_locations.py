"""Read-only filesystem diagnostics; these paths belong to the backend host."""
import json
from pathlib import Path


def image_storage_locations(image: dict, library: Path, database: Path) -> dict:
    library = library.resolve()

    def location(root, relative):
        if not relative:
            return {"path": None, "exists": False, "within_expected_root": True}
        root = root.resolve()
        path = (root / relative).resolve()
        safe = path.is_relative_to(root) and path.is_relative_to(library)
        return {"path": str(path), "exists": path.is_file() if safe else None,
                "within_expected_root": safe}

    trash = library / ".trash" / "{deletion-token}"
    deleted = image.get("folder_id") is None
    if deleted:
        for manifest in (library / ".trash").glob("*/manifest.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if any(row.get("image_id") == image["id"] for row in data.get("images", [])):
                    trash = manifest.parent
                    break
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    sidecar = str(Path(image["path"]).with_suffix(".txt"))
    files = {
        "image": location(library / "images", image["path"]),
        "thumbnail": location(library / "thumbnails", image.get("thumb_path")),
        "sidecar": location(library / "images", sidecar),
    }
    trash_files = {
        "image": location(trash / "images", image["path"]),
        "thumbnail": location(trash / "thumbnails", image.get("thumb_path")),
        "sidecar": location(trash / "images", sidecar),
        "manifest": location(trash, "manifest.json"),
    }
    return {
        "library_root": str(library), "files": files,
        "database": {"path": str(database.resolve()), "exists": database.is_file()},
        "metadata_records": {"image_id": image["id"], "source_ids": [s["id"] for s in image.get("sources", [])]},
        "trash": {"root": str(trash), "is_template": trash.name == "{deletion-token}", "files": trash_files},
        "scratch_root": str(library / ".scratch"), "deleted": deleted,
    }
