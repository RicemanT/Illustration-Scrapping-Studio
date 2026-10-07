"""Per-artist decisions from analysis results.

For one artist this finds the style to keep (the latest one; the artist's
main career style only when the latest era has too few usable images),
scores every analysed post by its distance to that style, applies the
content rules (sketch/rough, monochrome, comic, 3D, photo and AI images are
excluded unless they make up the majority of the kept style), ranks
aesthetics within the artist, drops near-duplicates and flags borderline
images for review. Only numpy is needed here.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.analysis.store import SCORERS
from app.services.post_dates import sortable_time

# Content categories: how a post is recognised and how it is reported.
CONTENT = {
    'rough': ('sketch or rough', lambda r: _value(r, 'rough') >= 0.5),
    'monochrome': ('monochrome', lambda r: max(_value(r, 'monochrome'), _value(r, 'mono')) >= 0.5),
    'comic': ('comic page', lambda r: _value(r, 'cls_comic') >= 0.5),
    '3d': ('3D render', lambda r: _value(r, 'cls_3d') >= 0.5),
    'photo': ('photo', lambda r: _value(r, 'real') >= 0.5),
    'ai': ('AI-generated', lambda r: _value(r, 'ai') >= 0.5),
}
AI_SUSPECT = 0.3


def _value(row: dict, column: str) -> float:
    value = row.get(column)
    return float(value) if value is not None else 0.0


@dataclass
class AssessOptions:
    keep_z: float = 2.5          # farther from the artist's style than this: off-style
    flag_z: float = 1.8          # between flag_z and keep_z: kept but flagged for review
    latest_posts: int = 0        # newest analysed posts defining the latest style (0 = half the target, 20 to 40)
    majority: float = 0.5        # a content category is kept when at least this share of the style has it
    aesthetic_floor: float = 0.2  # dataset-wide aesthetic percentile below which posts are dropped
    near_duplicate: float = 0.97  # deep-feature cosine above which two posts count as the same picture
    min_style_samples: int = 8    # fewer analysed posts than this: style is not judged


class AestheticScale:
    """Dataset-wide percentiles of every aesthetic scorer; the ensemble is their mean."""

    def __init__(self, columns: dict[str, list[float]]):
        self.sorted = {name: np.sort(np.asarray(values, dtype=np.float64)) for name, values in columns.items() if len(values) >= 2}

    @classmethod
    def load(cls, conn) -> 'AestheticScale':
        """Percentiles of the scorers ticked in the analysis settings (unticking one drops its stored scores too)."""
        from app.analysis.store import get_config
        config = get_config(conn)
        enabled = [name for name in SCORERS if getattr(config, 'deepghs' if name == 'dbaes' else name, True)]
        columns = {name: [] for name in enabled}
        for row in conn.execute(f"SELECT {', '.join(enabled)} FROM analysis_post WHERE status='done'"):
            for name in enabled:
                if row[name] is not None:
                    columns[name].append(row[name])
        return cls(columns)

    def percentile(self, name: str, value) -> Optional[float]:
        values = self.sorted.get(name)
        if values is None or value is None:
            return None
        low = np.searchsorted(values, value, side='left')
        high = np.searchsorted(values, value, side='right')
        return float((low + high) / 2 / len(values))

    def ensemble(self, row: dict) -> Optional[float]:
        parts = [p for p in (self.percentile(name, row.get(name)) for name in SCORERS) if p is not None]
        return float(np.mean(parts)) if parts else None

    def parts(self, row: dict) -> dict[str, float]:
        return {name: p for name in SCORERS if (p := self.percentile(name, row.get(name))) is not None}


def robust_center(vectors: np.ndarray, keep: float = 0.7, rounds: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Mean direction of the majority: repeatedly drop the farthest 30% and re-average."""
    vectors = _normalize(vectors)
    members = np.arange(len(vectors))
    center = _normalize(vectors.mean(axis=0, keepdims=True))[0]
    for _ in range(rounds):
        similarity = vectors @ center
        count = max(3, int(round(len(vectors) * keep)))
        members = np.argsort(-similarity)[:count]
        center = _normalize(vectors[members].mean(axis=0, keepdims=True))[0]
    return center, members


def robust_z(vectors: np.ndarray, center: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Distance to the style centre in robust standard deviations of the reference posts."""
    distance = 1.0 - _normalize(vectors) @ center
    ref = distance[reference]
    median = float(np.median(ref))
    spread = max(float(np.median(np.abs(ref - median))) * 1.4826, 0.01)
    return (distance - median) / spread


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1.0, norms)


def percentiles(values: list[float]) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < 2:
        return [1.0] * len(values)
    return [(bisect_left(ordered, v) + bisect_right(ordered, v) - 1) / 2 / (len(ordered) - 1) for v in values]


@dataclass
class Verdict:
    key: tuple
    reject: Optional[str] = None
    z: Optional[float] = None
    style: float = 0.5
    aesthetic: float = 0.5
    ensemble: Optional[float] = None
    categories: list = field(default_factory=list)
    flags: list = field(default_factory=list)


@dataclass
class ArtistAssessment:
    verdicts: dict
    era: str = 'latest'
    excluded: list = field(default_factory=list)
    kept_categories: list = field(default_factory=list)
    latest_usable: int = 0
    career_usable: int = 0
    judged_style: bool = True
    models: list = field(default_factory=list)


def _style_z(order: list[tuple], vectors: dict[str, dict], latest_count: int, era: str) -> dict[tuple, float]:
    """Combined z per post across the style models (mean of per-model z)."""
    per_model = []
    for model_vectors in vectors.values():
        keys = [key for key in order if key in model_vectors]
        if len(keys) < 3:
            continue
        matrix = np.stack([model_vectors[key] for key in keys])
        # The centre comes from the closest members (robust to other styles); the spread is measured over the
        # whole reference set, because the closest members alone understate an artist's natural variety.
        if era == 'latest':
            reference = np.arange(min(latest_count, len(keys)))  # `order` is newest first
            center, _ = robust_center(matrix[reference])
        else:
            # The career style is the majority mode: keep the closest half each round.
            reference = np.arange(len(keys))
            center, _ = robust_center(matrix, keep=0.5)
        per_model.append(dict(zip(keys, robust_z(matrix, center, reference))))
    combined = {}
    for key in order:
        values = [z[key] for z in per_model if key in z and np.isfinite(z[key])]
        if values:
            combined[key] = float(np.mean(values))
    return combined


def assess_artist(posts: list, analysis: dict, vectors: dict[str, dict], scale: AestheticScale, options: AssessOptions,
                  target: int, eligible: Optional[set] = None, duplicate_vectors: Optional[dict] = None,
                  needed: Optional[int] = None) -> Optional[ArtistAssessment]:
    """Decide for every post of one artist. `posts` are planner post rows; `eligible` are keys passing metadata filters.

    The latest style is kept unless it yields fewer than `needed` usable posts (default: half the target);
    then the artist's main career style is used if it yields more.

    Returns None when nothing of the artist was analysed (the planner then falls back to metadata only).
    """
    order = [(p['site'], str(p['remote_id'])) for p in sorted(posts, key=lambda p: sortable_time(p['created_at']), reverse=True)]
    analysed = [key for key in order if key in analysis]
    if not analysed:
        return None
    eligible = set(order) if eligible is None else eligible
    # The newest posts set the latest style's centre; any post close to it (older ones too) belongs to that style.
    latest_count = options.latest_posts or max(20, min(40, target // 2))
    judged = sum(1 for key in analysed if any(key in v for v in vectors.values())) >= options.min_style_samples
    categories = {key: [name for name, (_, test) in CONTENT.items() if test(analysis[key])] for key in analysed}
    ensemble = {key: scale.ensemble(analysis[key]) for key in analysed}

    def plan_for(era: str):
        z = _style_z(analysed, vectors, latest_count, era) if judged else {}
        in_style = [key for key in analysed if key in eligible and (not judged or z.get(key, 0.0) <= options.keep_z)]
        shares = {name: sum(name in categories[key] for key in in_style) / len(in_style) if in_style else 0.0 for name in CONTENT}
        excluded = [name for name, share in shares.items() if share < options.majority]
        usable = [key for key in in_style if not set(categories[key]) & set(excluded)
                  and (ensemble[key] is None or ensemble[key] >= options.aesthetic_floor)]
        return z, excluded, usable

    z, excluded, usable = plan_for('latest')
    result = ArtistAssessment(verdicts={}, latest_usable=len(usable), judged_style=judged,
                              models=[name for name, v in vectors.items() if any(key in v for key in analysed)])
    needed = needed if needed is not None else max(1, target // 2)
    if judged and len(usable) < needed:
        career = plan_for('career')
        result.career_usable = len(career[2])
        if len(career[2]) > len(usable):
            z, excluded, usable = career
            result.era = 'career'
    result.excluded = excluded
    result.kept_categories = [name for name in CONTENT if name not in excluded]

    usable_set = set(usable)
    ranks = dict(zip(usable, percentiles([ensemble[key] if ensemble[key] is not None else 0.5 for key in usable])))
    # Near-duplicates: keep the better-scored of two nearly identical pictures. Deep features carry content;
    # the style vectors average it away, so they are only a fallback.
    duplicate = set()
    first_model = duplicate_vectors if duplicate_vectors is not None else next(iter(vectors.values()), {})
    kept_vectors = []
    for key in sorted(usable, key=lambda k: -(ensemble[k] if ensemble[k] is not None else 0.5)):
        vector = first_model.get(key)
        if vector is not None:
            vector = vector / (np.linalg.norm(vector) or 1.0)
            if kept_vectors and float(np.max(np.stack(kept_vectors) @ vector)) >= options.near_duplicate:
                duplicate.add(key)
                continue
            kept_vectors.append(vector)

    for key in order:
        verdict = Verdict(key=key)
        if key not in analysis:
            verdict.reject = 'not_analyzed'
            result.verdicts[key] = verdict
            continue
        row = analysis[key]
        verdict.categories = categories[key]
        verdict.ensemble = ensemble[key]
        verdict.z = round(z[key], 3) if key in z else None
        if verdict.z is not None:
            verdict.style = float(np.clip(1.0 - max(verdict.z, 0.0) / options.keep_z, 0.0, 1.0))
        verdict.aesthetic = ranks.get(key, 0.0)
        hit = sorted(set(verdict.categories) & set(excluded))
        if judged and verdict.z is not None and verdict.z > options.keep_z:
            verdict.reject = 'off_style'
        elif hit:
            verdict.reject = f'content_{hit[0]}'
        elif verdict.ensemble is not None and verdict.ensemble < options.aesthetic_floor:
            verdict.reject = 'low_aesthetic'
        elif key in duplicate:
            verdict.reject = 'near_duplicate'
        # Review flags explain what to look at; they apply to kept and rejected posts alike.
        if verdict.z is not None and options.flag_z < verdict.z <= options.keep_z:
            verdict.flags.append('style_borderline')
        if verdict.z is not None and verdict.z > options.keep_z:
            verdict.flags.append('off_style')
        if hit:
            verdict.flags.extend(f'content_{name}' for name in hit)
        if 'ai' not in hit and _value(row, 'ai') >= AI_SUSPECT:
            verdict.flags.append('ai_suspect')
        if key in usable_set and ranks.get(key, 1.0) < 0.15:
            verdict.flags.append('low_aesthetic_here')
        if verdict.ensemble is not None and verdict.ensemble < options.aesthetic_floor:
            verdict.flags.append('low_aesthetic')
        if key in duplicate:
            verdict.flags.append('near_duplicate')
        result.verdicts[key] = verdict
    return result


FLAG_LABELS = {
    'off_style': 'Off-style', 'style_borderline': 'Borderline style', 'low_aesthetic': 'Low aesthetic (dataset)',
    'low_aesthetic_here': "Among the artist's weakest", 'near_duplicate': 'Near-duplicate', 'ai_suspect': 'Possibly AI',
    'not_analyzed': 'Not analysed', **{f'content_{name}': label.capitalize() for name, (label, _) in CONTENT.items()},
}


def auc(positive: list[float], negative: list[float]) -> Optional[float]:
    """Probability that a random positive scores above a random negative (ties count half)."""
    if not positive or not negative:
        return None
    ordered = sorted(negative)
    total = 0.0
    for value in positive:
        total += bisect_left(ordered, value) + (bisect_right(ordered, value) - bisect_left(ordered, value)) / 2
    return total / (len(positive) * len(negative))
