"""Dataset Planner selection: choose each artist's training images from post metadata.

Selection is a deterministic greedy optimizer. Every round, each artist adds the
remaining candidate with the largest marginal gain:

    gain = quality + novelty + character need + tag rarity + boost-tag bonus

Quality is a percentile within the artist, so small artists compete on equal
terms with famous ones. Novelty and character need shrink as images are chosen,
which makes the gain submodular and lets a lazy heap skip most recomputation.
Artists take turns (round-robin) so shared character floors are not consumed
by whichever artists happen to be processed first.

Characters are counted per tag family: Danbooru and Gelbooru share Danbooru's
tag names, while e621 has its own vocabulary.
"""
from __future__ import annotations

import heapq
import math
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pydantic import BaseModel, Field, field_validator

from app.services.media import ARCHIVE_FORMATS, PIL_IMAGE_FORMATS, VIDEO_FORMATS

# Formats the import pipeline can turn into training images. Videos, animated
# images and ugoira/archives are kept: import extracts up to three frames.
SUPPORTED_EXTS = PIL_IMAGE_FORMATS | VIDEO_FORMATS | ARCHIVE_FORMATS | {'jpg', 'apng'}
MOTION_EXTS = {'gif', 'apng'} | VIDEO_FORMATS | ARCHIVE_FORMATS
# Artist-category tags that are warnings or placeholders, not people
# (e621; verified against the live tag API 2026-10-02).
NON_ARTIST_TAGS = {
    'conditional_dnp', 'unknown_artist', 'anonymous_artist', 'sound_warning', 'epilepsy_warning',
    'third-party_edit', 'avoid_posting', 'unknown_artist_signature', 'jumpscare_warning',
}
FAMILIES = ('danbooru', 'e621')

# Default tag lists, verified on 2026-10-02 against the live Danbooru and e621
# tag APIs: every tag exists, is a current (non-deprecated, non-alias) name,
# and has the expected category. Tags are renamed over time, so the Planner
# page can re-check any list against the live sites.
DEFAULT_BLOCKED_TAGS = {
    'danbooru': ['comic', '4koma', '2koma', '3koma', 'multiple_4koma', 'text-only_page', 'ai-generated',
                 'ai-assisted', 'photo_(medium)', 'lowres', 'upscaled'],
    # e621 no longer accepts AI-generated uploads, so it has no AI tag to block.
    'e621': ['comic', 'low_res', 'compression_artifacts', 'upscale'],
}
# Style-dependent blocked tags: blocked only when they are a minority of the
# artist's work, so artists who mostly draw comics keep them.
ADAPTIVE_TAGS = {'comic', '4koma', '2koma', '3koma', 'multiple_4koma'}
# Useful but underrepresented concepts: camera and composition, action and
# poses, interaction, environments, vehicles, weather, lighting and effects.
# Each is on well under 2% of the site's posts.
DEFAULT_BOOST_TAGS = {
    'danbooru': [
        'from_below', 'from_above', 'foreshortening', 'fisheye', 'upside-down', 'sideways', 'wide_shot',
        'very_wide_shot', 'perspective', 'vanishing_point', 'isometric', 'close-up', 'pov_hands',
        'partially_underwater_shot', 'reflection',
        'dynamic_pose', 'fighting_stance', 'attacking_viewer', 'punching', 'kicking', 'high_kick', 'jumping',
        'midair', 'falling', 'running', 'flying', 'dancing', 'stretching', 'twisted_torso', 'climbing',
        'swimming', 'diving', 'riding', 'dual_wielding', 'aiming_at_viewer', 'motion_blur', 'speed_lines',
        'afterimage', 'explosion', 'splashing', 'hand_focus',
        'hug_from_behind', 'princess_carry', 'piggyback', 'lifting_person', 'fighting', 'battle', 'crowd',
        '6+girls', '6+boys',
        'scenery', 'landscape', 'cityscape', 'architecture', 'ruins', 'street', 'alley', 'mecha',
        'vehicle_focus', 'car', 'motorcycle', 'train', 'aircraft', 'ship', 'underwater', 'rain', 'snow',
        'fog', 'starry_sky', 'aurora', 'sunset', 'fireworks', 'lightning', 'magic_circle',
        'light_rays', 'sunbeam', 'dappled_sunlight', 'backlighting', 'silhouette', 'lens_flare', 'bokeh',
        'neon_lights', 'caustics',
    ],
    'e621': [
        'low-angle_view', 'high-angle_view', "worm's-eye_view", "bird's-eye_view", 'foreshortening',
        'dutch_angle', 'fisheye_lens', 'perspective', 'close-up', 'reflection',
        'action_pose', 'fighting_pose', 'fight', 'battle', 'jumping', 'midair', 'flying', 'running', 'falling',
        'dancing', 'stretching', 'climbing', 'swimming', 'speed_lines', 'motion_blur', 'impact_lines',
        'explosion', 'splash', 'hand_focus',
        'hugging_from_behind', 'carrying_another', 'piggyback', 'hand_holding', 'crowd',
        'scenery', 'landscape', 'cityscape', 'architecture', 'ruins', 'street', 'car', 'motorcycle', 'train',
        'aircraft', 'spacecraft', 'mecha', 'underwater', 'raining', 'snow', 'fog', 'starry_sky', 'sunset',
        'lightning', 'amazing_background',
        'light_beam', 'sunbeam', 'backlighting', 'rim_light', 'silhouette', 'lens_flare', 'bokeh',
        'depth_of_field',
    ],
}


CONTENT_GATES = ('rough', 'monochrome', 'comic', '3d', 'photo', 'ai')
DEFAULT_CONTENT_GATES = ('rough', 'monochrome', 'comic', '3d', 'photo')


class PlannerConfig(BaseModel):
    min_images: int = Field(20, ge=1, le=10000, description='Artists with fewer usable images are dropped')
    max_images: int = Field(60, ge=1, le=10000, description='Unique images per artist at most')
    exposures_per_artist: int = Field(200, ge=1, le=100000, description='Training samples per artist per plan pass')
    max_repeats: int = Field(10, ge=1, le=1000)
    candidate_pool: int = Field(300, ge=10, le=100000, description='Best candidates per artist kept for selection')
    min_short_side: int = Field(768, ge=0, le=20000)
    max_aspect_ratio: float = Field(2.5, ge=1.0, le=20.0)
    allowed_ratings: Optional[list[str]] = Field(None, description='None keeps every rating')
    include_motion: bool = Field(True, description='Keep videos, animations and ugoira; import extracts frames')
    max_credited_artists: int = Field(2, ge=1, le=100, description='Skip posts crediting more real artists (group collabs)')
    blocked_tags_danbooru: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_TAGS['danbooru']))
    blocked_tags_e621: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_TAGS['e621']))
    boost_tags_danbooru: list[str] = Field(default_factory=lambda: list(DEFAULT_BOOST_TAGS['danbooru']))
    boost_tags_e621: list[str] = Field(default_factory=lambda: list(DEFAULT_BOOST_TAGS['e621']))
    min_year: Optional[int] = Field(None, ge=1990, le=2100)
    newest_posts_per_artist: int = Field(0, ge=0, le=100000, description='Only consider each artist\'s newest N posts (0 = all)')
    character_floor: int = Field(30, ge=0, le=100000)
    character_share_cap: float = Field(0.3, gt=0, le=1.0, description='Largest share of one artist taken by one character')
    character_topup: bool = True
    topup_max_per_artist: int = Field(10, ge=0, le=10000)
    rarity_min_df: int = Field(20, ge=1, le=1000000, description='Ignore tags rarer than this when scoring rarity')
    # Measured on live Danbooru/e621 pools: heavier character/boost weights
    # crowded out artists' best original work; top-ups still fill floors.
    weight_quality: float = Field(1.0, ge=0, le=100)
    weight_novelty: float = Field(0.7, ge=0, le=100)
    weight_character: float = Field(0.3, ge=0, le=100)
    weight_rarity: float = Field(0.5, ge=0, le=100)
    weight_boost: float = Field(0.3, ge=0, le=100)
    priority_character_boost: float = Field(1.5, ge=1, le=10, description='Character-need multiplier for priority targets')
    content_majority: float = Field(0.5, ge=0.05, le=1.0, description="Comic-type tags and content classes are kept when at least this share of the artist's work has them")
    # Image analysis (DINO style, aesthetic scorers, content classifiers).
    use_analysis: bool = Field(False, description='Choose images with the image analysis results')
    style_keep_z: float = Field(2.5, ge=0.1, le=20, description='Farther from the artist style than this (robust z): off-style')
    style_flag_z: float = Field(1.8, ge=0.1, le=20, description='Kept but flagged for review above this distance')
    latest_style_posts: int = Field(0, ge=0, le=100000, description="Newest analysed posts that set the latest style's centre (0 = half the target, 20 to 40)")
    aesthetic_floor: float = Field(0.2, ge=0, le=0.95, description='Drop posts below this dataset-wide aesthetic percentile')
    near_duplicate: float = Field(0.97, ge=0.5, le=1.0, description='Deep-feature similarity above which two posts count as one picture')
    # Content types the classifiers may exclude (when a minority of the artist's style). AI is off by default:
    # artist lists usually avoid AI artists already, so its hits are mostly false positives.
    content_gates: list[str] = Field(default_factory=lambda: list(DEFAULT_CONTENT_GATES))
    weight_style: float = Field(1.0, ge=0, le=100)
    weight_aesthetic: float = Field(1.0, ge=0, le=100)

    @field_validator('content_gates')
    @classmethod
    def known_gates(cls, value):
        unknown = sorted(set(value) - set(CONTENT_GATES))
        if unknown:
            raise ValueError(f"Unknown content type(s): {', '.join(unknown)}")
        return [gate for gate in CONTENT_GATES if gate in value]

    def tag_set(self, kind: str, family: str) -> set[str]:
        return {tag.strip().replace(' ', '_') for tag in getattr(self, f'{kind}_tags_{family}') if tag.strip()}


def split(value: Optional[str]) -> tuple[str, ...]:
    return tuple(sys.intern(tag) for tag in (value or '').split())


def real_artists(row) -> list[str]:
    return [tag for tag in split(row['artists']) if tag not in NON_ARTIST_TAGS]


def rejection(row, config: PlannerConfig, blocked: set[str], banned: bool = False) -> Optional[str]:
    """Return why a post can never be selected, or None when it is usable."""
    if banned:
        return 'banned_by_user'
    if not row['file_url']:
        return 'no_file'
    ext = (row['ext'] or '').lower()
    if ext not in SUPPORTED_EXTS:
        return 'unsupported_format'
    if ext in MOTION_EXTS and not config.include_motion:
        return 'motion'
    width, height = row['width'] or 0, row['height'] or 0
    if min(width, height) < max(config.min_short_side, 1):
        return 'low_resolution'
    if max(width, height) / min(width, height) > config.max_aspect_ratio:
        return 'aspect_ratio'
    if config.allowed_ratings and row['rating'] not in config.allowed_ratings:
        return 'rating'
    if blocked and blocked.intersection(split(row['general']) + split(row['meta'])):
        return 'blocked_tag'
    if len(real_artists(row)) > config.max_credited_artists:
        return 'too_many_artists'
    if config.min_year:
        from app.services.post_dates import post_year
        year = post_year(row['created_at'])
        if year and year < config.min_year:
            return 'too_old'
    return None


@dataclass(slots=True)
class Candidate:
    site: str
    remote_id: str
    artist_id: int
    md5: Optional[str]
    family: str
    short_side: int
    popularity: float
    characters: tuple
    content: tuple
    general: tuple
    locked: bool = False
    quality: float = 0.0
    rarity: float = 0.0
    boost: float = 0.0
    prefilter: float = 0.0
    style: float = 0.0
    aesthetic: float = 0.0

    @property
    def key(self) -> tuple[str, str]:
        return self.site, self.remote_id


def candidate_from_row(row, locked: bool = False) -> Candidate:
    """Parent/child variants of the same characters share a family key so only one of them can be chosen.

    Artists often hang a whole cast under one parent post (character sprites, a set of portraits); children
    showing other characters are different pictures, so the characters are part of the key.
    """
    parent = row['parent_id'] if row['parent_id'] not in (None, '', '0') else None
    characters = split(row['characters'])
    general = split(row['general']) + split(row['species'])
    popularity = row['fav_count'] if row['fav_count'] is not None else (row['score'] or 0)
    return Candidate(
        site=row['site'], remote_id=str(row['remote_id']), artist_id=row['artist_id'], md5=row['md5'],
        family=f"{row['site']}:{parent or row['remote_id']}:{' '.join(sorted(characters))}", short_side=min(row['width'] or 0, row['height'] or 0),
        popularity=float(popularity), characters=characters, content=general + characters + split(row['copyrights']),
        general=general, locked=locked,
    )


class RarityIndex:
    """Normalized inverse document frequency over one tag family's usable posts."""

    def __init__(self, min_df: int):
        self.min_df = min_df
        self.df: Counter = Counter()
        self.documents = 0

    def add(self, tags: Iterable[str]) -> None:
        self.documents += 1
        self.df.update(set(tags))

    def score(self, tags: Iterable[str]) -> float:
        top = math.log(max(self.documents, 2) / self.min_df)
        if top <= 0:
            return 0.0
        values = sorted((min(1.0, math.log(self.documents / self.df[t]) / top)
                         for t in set(tags) if self.df[t] >= self.min_df), reverse=True)[:3]
        return sum(values) / 3


def percentiles(values: list[float]) -> list[float]:
    """Rank-normalize to [0, 1]; ties share their average rank."""
    ordered = sorted(values)
    if len(ordered) < 2:
        return [1.0] * len(values)
    return [(bisect_left(ordered, v) + bisect_right(ordered, v) - 1) / 2 / (len(ordered) - 1) for v in values]


def score_artist(candidates: list[Candidate], rarity: RarityIndex, boost: set[str], targets: set[str], config: PlannerConfig) -> list[Candidate]:
    """Fill static scores and keep the artist's best `candidate_pool` candidates plus locks.

    Quality and rarity are percentiles within the artist: raw tag rarity barely
    varies across a large corpus, but each artist still has unusual pieces.
    """
    popularity = percentiles([c.popularity for c in candidates])
    rarities = percentiles([rarity.score(c.general) for c in candidates])
    for c, pop, rare in zip(candidates, popularity, rarities):
        c.quality = 0.8 * pop + 0.2 * min(1.0, c.short_side / 1024)
        c.rarity = rare
        c.boost = 1.0 if boost.intersection(c.general) else 0.0
        c.prefilter = (c.quality + 0.5 * c.rarity + 0.5 * c.boost + (0.5 if targets.intersection(c.characters) else 0.0)
                       + config.weight_style * c.style + config.weight_aesthetic * c.aesthetic)
    locked = [c for c in candidates if c.locked]
    rest = sorted((c for c in candidates if not c.locked), key=lambda c: (-c.prefilter, c.remote_id))
    return locked + rest[:max(config.candidate_pool - len(locked), 0)]


@dataclass
class ArtistPlan:
    artist_id: int
    quota: int
    usable: int
    pool: list[Candidate]
    picked: list[tuple[Candidate, dict]] = field(default_factory=list)
    cover: Counter = field(default_factory=Counter)
    character_share: Counter = field(default_factory=Counter)
    families: set = field(default_factory=set)
    heap: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    topups: int = 0


@dataclass
class FamilyResult:
    artists: dict = field(default_factory=dict)
    picks: list = field(default_factory=list)
    character_counts: Counter = field(default_factory=Counter)
    unmet_characters: list = field(default_factory=list)


class Selector:
    def __init__(self, config: PlannerConfig, targets: set[str], priority: set[str] = frozenset()):
        self.config = config
        self.targets = targets
        self.priority = priority
        self.character_counts: Counter = Counter()
        self.used_md5: set = set()

    def share_limit(self, plan: ArtistPlan) -> int:
        return max(2, math.ceil(self.config.character_share_cap * plan.quota))

    def components(self, c: Candidate, plan: ArtistPlan) -> dict:
        cfg = self.config
        if c.content:
            novelty = sum(1.0 / (1 + plan.cover[t]) for t in c.content) / len(c.content) * min(1.0, len(c.content) / 12)
        else:
            novelty = 0.0
        floor = cfg.character_floor
        need = max(((floor - self.character_counts[ch]) / floor * (cfg.priority_character_boost if ch in self.priority else 1.0)
                    for ch in c.characters if ch in self.targets and self.character_counts[ch] < floor), default=0.0) if floor else 0.0
        gain = (cfg.weight_quality * c.quality + cfg.weight_novelty * novelty + cfg.weight_character * need
                + cfg.weight_rarity * c.rarity + cfg.weight_boost * c.boost
                + cfg.weight_style * c.style + cfg.weight_aesthetic * c.aesthetic)
        reasons = {'gain': round(gain, 4), 'quality': round(c.quality, 4), 'novelty': round(novelty, 4),
                   'character_need': round(need, 4), 'rarity': round(c.rarity, 4), 'boost': c.boost}
        if cfg.use_analysis:
            reasons.update(style=round(c.style, 4), aesthetic=round(c.aesthetic, 4))
        return reasons

    def available(self, c: Candidate, plan: ArtistPlan) -> bool:
        return c.family not in plan.families and (not c.md5 or c.md5 not in self.used_md5)

    def within_share(self, c: Candidate, plan: ArtistPlan) -> bool:
        limit = self.share_limit(plan)
        return all(plan.character_share[ch] < limit for ch in c.characters)

    def accept(self, c: Candidate, plan: ArtistPlan, reasons: dict, role: str) -> None:
        plan.picked.append((c, {**reasons, 'role': role}))
        plan.cover.update(c.content)
        plan.character_share.update(c.characters)
        plan.families.add(c.family)
        self.character_counts.update(c.characters)
        if c.md5:
            self.used_md5.add(c.md5)

    def step(self, plan: ArtistPlan) -> bool:
        """Lazy greedy pick. Stale heap values are upper bounds because gains only shrink."""
        while plan.heap:
            _, order, c = heapq.heappop(plan.heap)
            if not self.available(c, plan):
                continue
            if not self.within_share(c, plan):
                plan.deferred.append((order, c))
                continue
            reasons = self.components(c, plan)
            if not plan.heap or reasons['gain'] >= -plan.heap[0][0] - 1e-9:
                self.accept(c, plan, reasons, 'selected')
                return True
            heapq.heappush(plan.heap, (-reasons['gain'], order, c))
        # Single-character artists may not fill their quota under the share cap.
        best = None
        for order, c in plan.deferred:
            if self.available(c, plan):
                reasons = self.components(c, plan)
                if best is None or (reasons['gain'], -order) > (best[0]['gain'], -best[1]):
                    best = (reasons, order, c)
        if best:
            plan.deferred = [(o, c) for o, c in plan.deferred if c is not best[2]]
            self.accept(best[2], plan, {**best[0], 'share_cap_relaxed': True}, 'selected')
            return True
        return False

    def select(self, plans: list[ArtistPlan]) -> None:
        active = []
        for plan in plans:
            for c in plan.pool:
                if c.locked and self.available(c, plan):
                    self.accept(c, plan, {**self.components(c, plan), 'locked': True}, 'locked')
            for order, c in enumerate(plan.pool):
                if not c.locked:
                    plan.heap.append((-self.components(c, plan)['gain'], order, c))
            heapq.heapify(plan.heap)
            if len(plan.picked) < plan.quota:
                active.append(plan)
        rotation = 0
        while active:
            still = []
            # Rotate the starting artist so no artist always picks first.
            start = rotation % len(active)
            for plan in active[start:] + active[:start]:
                if len(plan.picked) < plan.quota and self.step(plan) and len(plan.picked) < plan.quota:
                    still.append(plan)
            active = sorted(still, key=lambda p: p.artist_id)
            rotation += 1

    def top_up(self, plans: list[ArtistPlan], ranked_targets: list[str]) -> None:
        """Add unselected pool images for characters still below their floor."""
        floor = self.config.character_floor
        by_character = defaultdict(list)
        for plan in plans:
            chosen = {c.key for c, _ in plan.picked}
            for c in plan.pool:
                if c.key not in chosen:
                    for ch in c.characters:
                        if ch in self.targets:
                            by_character[ch].append((plan, c))
        for ch in ranked_targets:
            options = sorted(by_character.get(ch, ()), key=lambda item: (-item[1].quality, item[1].remote_id))
            for plan, c in options:
                if self.character_counts[ch] >= floor:
                    break
                if plan.topups >= self.config.topup_max_per_artist or not self.available(c, plan):
                    continue
                plan.topups += 1
                self.accept(c, plan, {**self.components(c, plan), 'topup_for': ch}, 'character_topup')


def plan_family(artists: dict[int, list[Candidate]], usable_counts: dict[int, int], config: PlannerConfig,
                ranked_targets: list[str], priority: set[str] = frozenset()) -> FamilyResult:
    """Select images for every artist in one tag family.

    `artists` maps artist IDs to scored candidate pools; `usable_counts` gives
    each artist's usable posts after collapsing parent/child families.
    `ranked_targets` lists characters by priority (priority targets first).
    """
    targets = set(ranked_targets)
    selector = Selector(config, targets, priority)
    result = FamilyResult()
    plans = []
    for artist_id in sorted(artists):
        usable = usable_counts.get(artist_id, 0)
        locks = sum(c.locked for c in artists[artist_id])
        if usable < config.min_images and not locks:
            result.artists[artist_id] = {'status': 'dropped', 'reason': 'too_few_usable', 'usable': usable, 'selected': 0, 'repeats': 0}
            continue
        plans.append(ArtistPlan(artist_id, max(min(config.max_images, usable), locks), usable, artists[artist_id]))
    selector.select(plans)
    kept = []
    for plan in plans:
        if len(plan.picked) < config.min_images and not any(c.locked for c, _ in plan.picked):
            for c, _ in plan.picked:
                selector.character_counts.subtract(c.characters)
                if c.md5:
                    selector.used_md5.discard(c.md5)
            result.artists[plan.artist_id] = {'status': 'dropped', 'reason': 'too_few_after_selection', 'usable': plan.usable, 'selected': 0, 'repeats': 0}
        else:
            kept.append(plan)
    if config.character_topup and config.character_floor:
        selector.top_up(kept, ranked_targets)
    for plan in kept:
        repeats = min(config.max_repeats, max(1, round(config.exposures_per_artist / len(plan.picked))))
        result.artists[plan.artist_id] = {'status': 'kept', 'reason': None, 'usable': plan.usable,
                                          'selected': len(plan.picked), 'repeats': repeats, 'topups': plan.topups}
        for order, (c, reasons) in enumerate(plan.picked):
            result.picks.append({'artist_id': plan.artist_id, 'site': c.site, 'remote_id': c.remote_id,
                                 'role': reasons.pop('role'), 'pick_order': order, 'repeats': repeats,
                                 'gain': reasons['gain'], 'reasons': reasons})
    result.character_counts = Counter({ch: n for ch, n in selector.character_counts.items() if n > 0})
    if config.character_floor:
        result.unmet_characters = [(ch, result.character_counts.get(ch, 0)) for ch in ranked_targets
                                   if result.character_counts.get(ch, 0) < config.character_floor]
    return result
