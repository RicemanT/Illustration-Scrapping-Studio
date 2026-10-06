"""Analysis results as seen by the planner, review flags and aesthetic tags."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from app.analysis.assess import AestheticScale, AssessOptions, assess_artist
from app.analysis.store import STYLE_MODELS, connect, get_config, load_posts, load_vectors, vector_key


def options_from(config) -> AssessOptions:
    return AssessOptions(keep_z=config.style_keep_z, flag_z=config.style_flag_z, latest_posts=config.latest_style_posts,
                         majority=config.content_majority, aesthetic_floor=config.aesthetic_floor, near_duplicate=config.near_duplicate)


class AnalysisContext:
    """One open analysis database plus the dataset-wide aesthetic scale (loaded once)."""

    def __init__(self, planner_config, style_range: Optional[str] = None):
        self.conn = connect()
        settings = get_config(self.conn)
        self.style_keys = {model: vector_key(model, style_range or settings.style_range) for model in STYLE_MODELS}
        # The deepest stored block range describes content best: used for near-duplicates.
        self.duplicate_key = vector_key('dinov2' if settings.dinov2 else 'dinov3', settings.dino_ranges[-1])
        self.scale = AestheticScale.load(self.conn)
        self.options = options_from(planner_config)
        # The latest style is enough when it gives at least half the target (and the minimum per artist).
        self.needed = max(planner_config.min_images, planner_config.max_images // 2)
        self.missing = 0
        self.eras = Counter()
        self.excluded = Counter()

    def assess(self, rows: list, eligible: Optional[set], target: int, style_keys: Optional[dict] = None):
        keys = [(r['site'], str(r['remote_id'])) for r in rows]
        analysis = load_posts(self.conn, keys)
        if not analysis:
            self.missing += 1
            return None
        vectors = {}
        for model, key in (style_keys or self.style_keys).items():
            found = load_vectors(self.conn, list(analysis), key)
            if found:
                vectors[model] = found
        duplicates = load_vectors(self.conn, list(analysis), self.duplicate_key) or None
        assessment = assess_artist(rows, analysis, vectors, self.scale, self.options, target, eligible, duplicates, self.needed)
        if assessment is not None:
            self.eras[assessment.era] += 1
            self.excluded.update(assessment.excluded)
        return assessment, analysis

    def summary(self) -> dict:
        return {'artists_without_analysis': self.missing, 'eras': dict(self.eras), 'excluded_content': dict(self.excluded)}

    def close(self) -> None:
        self.conn.close()
