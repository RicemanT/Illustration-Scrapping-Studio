"""Offline microbenchmark: python benchmarks/quality_gate.py (from backend)."""
import sys
import tempfile
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageOps
from app.services.media import ensure_image_meets_folder_quality


def old_gate(path):
    with Image.open(path) as source:
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
        oriented.close()
    assert width >= 512 and height >= 512


def measure(function, path):
    samples = []
    for _ in range(12):
        start = time.perf_counter()
        function(path)
        samples.append((time.perf_counter() - start) * 1000)
    return median(samples)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        for extension in ("jpg", "png", "webp"):
            path = Path(directory) / f"fixture.{extension}"
            with Image.effect_noise((2400, 1600), 30).convert("RGB") as fixture:
                fixture.save(path)
            before = measure(old_gate, path)
            after = measure(lambda file: ensure_image_meets_folder_quality(str(file), {}), path)
            print(f"{extension}: old dimension gate {before:.2f} ms; new {after:.2f} ms; {before / max(after, 0.0001):.1f}x for this stage only")
