"""Review flags for planner collections, calibration against hand curation, and the curation reset."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Optional

import numpy as np

import app.db as db
from app.analysis.assess import FLAG_LABELS, _style_z, auc
from app.analysis.planning import AnalysisContext
from app.analysis.store import SCORERS, SCORER_LABELS, connect as analysis_connect, load_posts, load_vectors
from app.services.planner_select import ADAPTIVE_TAGS, PlannerConfig, rejection
from app.services.planner_store import connect as planner_connect, now, planner_dir
from app.services.post_dates import sortable_time


def _latest_config(planner) -> PlannerConfig:
    row = planner.execute("SELECT config FROM run WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone()
    return PlannerConfig.model_validate(json.loads(row['config'])) if row else PlannerConfig()


def _eligible(rows, config: PlannerConfig, family: str, bans: set) -> set:
    blocked = config.tag_set('blocked', family)
    tagged = sum(1 for r in rows if ADAPTIVE_TAGS.intersection((r['general'] or '').split() + (r['meta'] or '').split()))
    if rows and tagged / len(rows) >= config.content_majority:
        blocked = blocked - ADAPTIVE_TAGS
    return {(r['site'], str(r['remote_id'])) for r in rows if rejection(r, config, blocked, banned=(r['site'], r['remote_id']) in bans) is None}


def _present(main, folder_id: int) -> dict[int, tuple]:
    images = {}
    for r in main.execute("""SELECT i.id, s.provider, s.remote_id FROM image i JOIN image_source s ON s.image_id=i.id
                             WHERE i.folder_id=? ORDER BY s.id""", (folder_id,)):
        images.setdefault(r['id'], (r['provider'], str(r['remote_id']).split(':', 1)[0]))
    return images


def folder_review(folder_id: int) -> Optional[dict]:
    """Flags and model results for every image in a planner collection; stores the flags on the images."""
    planner = planner_connect()
    try:
        link = planner.execute('SELECT artist_id FROM delivery_item WHERE folder_id=? LIMIT 1', (folder_id,)).fetchone()
        if not link:
            return None
        artist = planner.execute('SELECT * FROM artist WHERE id=?', (link['artist_id'],)).fetchone()
        config = _latest_config(planner)
        rows = planner.execute('SELECT * FROM post WHERE artist_id=?', (artist['id'],)).fetchall()
        bans = {(r['site'], r['remote_id']) for r in planner.execute("SELECT site, remote_id FROM override WHERE artist_id=? AND action='ban'", (artist['id'],))}
    finally:
        planner.close()
    family = 'e621' if artist['site'] == 'e621' else 'danbooru'
    main = db.get_connection()
    try:
        images = _present(main, folder_id)
        context = AnalysisContext(config)
        try:
            assessed = context.assess(rows, _eligible(rows, config, family, bans), config.max_images)
            scale = context.scale
        finally:
            context.close()
        result = {'folder_id': folder_id, 'analysed': bool(assessed), 'images': {}, 'counts': {}, 'labels': FLAG_LABELS,
                  'scorers': SCORER_LABELS}
        if not assessed:
            main.execute('UPDATE image SET analysis_flags=NULL, analysis_flag_count=NULL WHERE folder_id=?', (folder_id,))
            main.commit()
            return result
        assessment, analysis = assessed
        result.update(era=assessment.era, excluded=assessment.excluded, kept_categories=assessment.kept_categories,
                      judged_style=assessment.judged_style, models=assessment.models,
                      latest_usable=assessment.latest_usable, career_usable=assessment.career_usable)
        counts = Counter()
        updates = []
        severity = {'off_style': 3, 'low_aesthetic': 3, 'near_duplicate': 2, 'ai_suspect': 2,
                    'style_borderline': 1, 'low_aesthetic_here': 1, 'not_analyzed': 1}
        for image_id, key in images.items():
            verdict = assessment.verdicts.get(key)
            row = analysis.get(key)
            flags = list(verdict.flags) if verdict else ['not_analyzed']
            if verdict is None and row is None:
                flags = ['not_analyzed']
            counts.update(flags)
            detail = {'flags': flags}
            if verdict is not None and row is not None:
                detail.update(z=verdict.z, style=round(verdict.style, 3), aesthetic_rank=round(verdict.aesthetic, 3),
                              ensemble=round(verdict.ensemble, 4) if verdict.ensemble is not None else None,
                              would_reject=verdict.reject, categories=verdict.categories,
                              scores={name: row.get(name) for name in SCORERS if row.get(name) is not None},
                              percentiles={name: round(p, 4) for name, p in scale.parts(row).items()},
                              classes={name: row.get(name) for name in ('polished', 'rough', 'monochrome', 'mono', 'cls_illustration', 'cls_comic',
                                                                        'cls_3d', 'cls_bangumi', 'real', 'ai') if row.get(name) is not None},
                              era=row.get('era'), era_conf=row.get('era_conf'))
            result['images'][image_id] = detail
            # Flags-first order: the most serious flag first, then how many.
            priority = 10 * max((severity.get(flag, 3) for flag in flags), default=0) + len(flags) if flags else 0
            updates.append((json.dumps(flags) if flags else None, priority, image_id))
        main.executemany('UPDATE image SET analysis_flags=?, analysis_flag_count=? WHERE id=?', updates)
        main.commit()
        result['counts'] = dict(counts)
        return result
    finally:
        main.close()


# --- Calibration against hand curation -------------------------------------------------------

def curation_labels(planner, main, artist_ids: Optional[set] = None) -> dict[int, dict]:
    """Per curated artist: posts kept in the collection, posts removed by hand, and hand-set marks."""
    labels: dict[int, dict] = {}
    folders = {}
    for r in planner.execute("SELECT DISTINCT folder_id, artist_id FROM delivery_item"):
        if artist_ids is None or r['artist_id'] in artist_ids:
            folders.setdefault(r['folder_id'], r['artist_id'])
    for folder_id, artist_id in folders.items():
        if not main.execute('SELECT 1 FROM collection WHERE id=?', (folder_id,)).fetchone():
            continue
        present = set(_present(main, folder_id).values())
        delivered = {(r['site'], r['remote_id']) for r in planner.execute(
            "SELECT site, remote_id FROM delivery_item WHERE folder_id=? AND status IN ('done', 'skipped')", (folder_id,))}
        marks = {}
        for r in main.execute("""SELECT s.provider, s.remote_id, i.quality_mark, i.quality_source, i.aesthetic_mark, i.aesthetic_source
                                 FROM image i JOIN image_source s ON s.image_id=i.id WHERE i.folder_id=?""", (folder_id,)):
            key = f"{r['provider']}:{str(r['remote_id']).split(':', 1)[0]}"
            marks[key] = {'quality': r['quality_mark'] if r['quality_source'] == 'manual' else None,
                          'aesthetic': r['aesthetic_mark'] if r['aesthetic_source'] == 'manual' else None}
        completed = planner.execute('SELECT completed_at FROM artist WHERE id=?', (artist_id,)).fetchone()['completed_at']
        removed = sorted(delivered - present)
        if removed or completed or any(m['quality'] or m['aesthetic'] for m in marks.values()):
            labels[artist_id] = {'folder_id': folder_id, 'kept': sorted(f'{s}:{i}' for s, i in present),
                                 'removed': [f'{s}:{i}' for s, i in removed], 'marks': marks, 'completed': bool(completed)}
    return labels


def _keys(items) -> list[tuple]:
    return [tuple(item.split(':', 1)) for item in items]


def latest_backup_labels() -> Optional[dict]:
    backups = sorted((planner_dir() / 'exports').glob('curation-backup-*.json'))
    for path in reversed(backups):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('labels'):
            return {int(k): v for k, v in data['labels'].items()}
    return None


def calibration(source: str = 'auto') -> dict:
    """How well each style model and scorer agrees with what was removed and marked by hand."""
    planner = planner_connect()
    main = db.get_connection()
    try:
        labels = latest_backup_labels() if source in ('auto', 'backup') else None
        used = 'backup' if labels else 'live'
        if not labels:
            labels = curation_labels(planner, main)
        rows_by_artist = {artist_id: planner.execute('SELECT site, remote_id, created_at FROM post WHERE artist_id=?', (artist_id,)).fetchall()
                          for artist_id in labels}
        config = _latest_config(planner)
    finally:
        main.close()
        planner.close()
    conn = analysis_connect()
    try:
        vector_keys = [r[0] for r in conn.execute('SELECT DISTINCT model FROM analysis_vector')]
        style = defaultdict(lambda: {'pos': [], 'neg': [], 'artists': 0})
        scorer = defaultdict(lambda: {'pos': [], 'neg': []})
        quality = defaultdict(lambda: {'pos': [], 'neg': []})
        aesthetic = defaultdict(lambda: {'pos': [], 'neg': []})
        bands = defaultdict(lambda: {'kept': 0, 'removed': 0})
        context = AnalysisContext(config)
        scale = context.scale
        context.close()
        artists = 0
        for artist_id, label in labels.items():
            kept, removed = _keys(label['kept']), _keys(label['removed'])
            rows = rows_by_artist[artist_id]
            order = [(r['site'], str(r['remote_id'])) for r in sorted(rows, key=lambda r: sortable_time(r['created_at']), reverse=True)]
            analysis = load_posts(conn, order)
            if not analysis:
                continue
            artists += 1
            analysed = [key for key in order if key in analysis]
            latest = config.latest_style_posts or max(20, min(40, config.max_images // 2))
            ranges = defaultdict(dict)
            for key_name in vector_keys:
                vectors = load_vectors(conn, analysed, key_name)
                if len(vectors) < 8:
                    continue
                z = _style_z(analysed, {key_name: vectors}, latest, 'latest')
                ranges[key_name.split(':', 1)[1]][key_name.split(':', 1)[0]] = vectors
                bucket = style[key_name]
                bucket['pos'] += [z[k] for k in removed if k in z]
                bucket['neg'] += [z[k] for k in kept if k in z]
                bucket['artists'] += 1
            for block_range, models in ranges.items():
                if len(models) > 1:
                    z = _style_z(analysed, models, latest, 'latest')
                    bucket = style[f'dinov2+dinov3:{block_range}']
                    bucket['pos'] += [z[k] for k in removed if k in z]
                    bucket['neg'] += [z[k] for k in kept if k in z]
                    bucket['artists'] += 1
            for keys, side in ((kept, 'kept'), (removed, 'removed')):
                for key in keys:
                    band = (analysis.get(key) or {}).get('anzhc_class')
                    if band is not None:
                        bands[int(band)][side] += 1
            for name in (*SCORERS, 'ensemble'):
                def value(key):
                    row = analysis.get(key)
                    if row is None:
                        return None
                    return scale.ensemble(row) if name == 'ensemble' else scale.percentile(name, row.get(name))
                # Higher means "kept"; AUC: kept above removed.
                scorer[name]['pos'] += [v for k in kept if (v := value(k)) is not None]
                scorer[name]['neg'] += [v for k in removed if (v := value(k)) is not None]
                for key_text, mark in label['marks'].items():
                    key = tuple(key_text.split(':', 1))
                    v = value(key)
                    if v is None:
                        continue
                    if mark.get('quality'):
                        quality[name]['pos' if mark['quality'] in ('masterpiece', 'best quality') else 'neg'].append(v)
                    if mark.get('aesthetic') or mark.get('quality'):
                        aesthetic[name]['pos' if mark.get('aesthetic') in ('very aesthetic', 'aesthetic') else 'neg'].append(v)
        report_style = sorted(({'model': name, 'auc': auc(b['pos'], b['neg']), 'removed': len(b['pos']), 'kept': len(b['neg']), 'artists': b['artists']}
                               for name, b in style.items()), key=lambda r: -(r['auc'] or 0))
        report_scorers = [{'scorer': name, 'label': SCORER_LABELS.get(name, 'Ensemble of all scorers'),
                           'kept_vs_removed': auc(scorer[name]['pos'], scorer[name]['neg']),
                           'quality_marks': auc(quality[name]['pos'], quality[name]['neg']),
                           'aesthetic_marks': auc(aesthetic[name]['pos'], aesthetic[name]['neg'])} for name in (*SCORERS, 'ensemble')]
        return {'source': used, 'artists': artists, 'labelled_artists': len(labels),
                'removed': sum(len(l['removed']) for l in labels.values()), 'kept': sum(len(l['kept']) for l in labels.values()),
                'style': report_style, 'scorers': report_scorers,
                'anzhc_bands': [{'band': band, **counts} for band, counts in sorted(bands.items())],
                'note': 'AUC 0.5 = no better than chance, 1.0 = perfect. Style: how well distance from the artist style separates what you removed from what you kept.'}
    finally:
        conn.close()


# --- Redo: back up and reset hand curation ----------------------------------------------------

def reset_curation(artist_ids: Optional[list[int]] = None) -> dict:
    """Back up every hand decision, then clear locks, bans, acceptance, marks and eras so the plan starts fresh.

    Images removed by hand are marked `removed` on their delivery items, so the next plan does not ban them
    again and they can be chosen by the analysis. The backup keeps them as calibration labels.
    """
    planner = planner_connect()
    main = db.get_connection()
    try:
        scope = set(artist_ids) if artist_ids else {r[0] for r in planner.execute('SELECT DISTINCT artist_id FROM delivery_item')}
        labels = curation_labels(planner, main, scope)
        folders = {r['folder_id']: r['artist_id'] for r in planner.execute('SELECT DISTINCT folder_id, artist_id FROM delivery_item') if r['artist_id'] in scope}
        backup = {
            'created_at': now(), 'artists': sorted(scope), 'labels': {str(k): v for k, v in labels.items()},
            'overrides': [dict(r) for r in planner.execute(f"SELECT * FROM override WHERE artist_id IN ({','.join('?' * len(scope)) or 'NULL'})", list(scope))],
            'accepted_posts': [dict(r) for r in planner.execute(f"SELECT * FROM accepted_post WHERE artist_id IN ({','.join('?' * len(scope)) or 'NULL'})", list(scope))],
            'completed': {str(r['id']): r['completed_at'] for r in planner.execute(f"SELECT id, completed_at FROM artist WHERE id IN ({','.join('?' * len(scope)) or 'NULL'})", list(scope))},
            'images': [dict(r) for r in main.execute(
                f"""SELECT id, folder_id, review_status, quality_mark, quality_source, aesthetic_mark, aesthetic_source, marks_viewed_at, quality_tags
                    FROM image WHERE folder_id IN ({','.join('?' * len(folders)) or 'NULL'})""", list(folders))],
            'eras': {str(r['id']): r['era_from'] for r in main.execute(
                f"SELECT id, era_from FROM collection WHERE id IN ({','.join('?' * len(folders)) or 'NULL'})", list(folders))},
        }
        path = planner_dir() / 'exports' / f"curation-backup-{now().replace(':', '').replace('-', '')[:15]}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(backup, ensure_ascii=False), encoding='utf-8')

        placeholders = ','.join('?' * len(scope)) or 'NULL'
        removed_items = 0
        for artist_id, label in labels.items():
            for item in label['removed']:
                site, remote_id = item.split(':', 1)
                removed_items += planner.execute("UPDATE delivery_item SET status='removed' WHERE folder_id=? AND site=? AND remote_id=? AND status IN ('done', 'skipped')",
                                                 (label['folder_id'], site, remote_id)).rowcount
        overrides = planner.execute(f'DELETE FROM override WHERE artist_id IN ({placeholders})', list(scope)).rowcount
        planner.execute(f'DELETE FROM accepted_post WHERE artist_id IN ({placeholders})', list(scope))
        completed = planner.execute(f'UPDATE artist SET completed_at=NULL WHERE id IN ({placeholders}) AND completed_at IS NOT NULL', list(scope)).rowcount
        planner.commit()
        folder_placeholders = ','.join('?' * len(folders)) or 'NULL'
        touched = [r[0] for r in main.execute(f'SELECT id FROM image WHERE folder_id IN ({folder_placeholders}) AND quality_tags IS NOT NULL', list(folders))]
        main.execute(f"""UPDATE image SET review_status='pending', quality_mark=NULL, quality_source=NULL, aesthetic_mark=NULL, aesthetic_source=NULL,
                         marks_viewed_at=NULL, quality_tags=NULL WHERE folder_id IN ({folder_placeholders})""", list(folders))
        main.execute(f'UPDATE collection SET era_from=NULL WHERE id IN ({folder_placeholders})', list(folders))
        main.commit()
    finally:
        main.close()
        planner.close()
    from app.services.tags import TagService
    TagService(db.LIBRARY_PATH)._rewrite_sidecars(touched)
    return {'backup': str(path), 'artists': len(scope), 'folders': len(folders), 'overrides_cleared': overrides,
            'completed_cleared': completed, 'removals_released': removed_items, 'sidecars_rewritten': len(touched)}
