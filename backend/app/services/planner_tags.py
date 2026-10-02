"""Live tag lookups for the Dataset Planner.

Tag vocabularies change: tags are renamed (aliased), deprecated or removed.
`check_tags` reports the current state of a tag list on Danbooru (also used by
Gelbooru) or e621. `top_characters` builds character targets from the sites'
own post counts, and `series_characters` lists the characters of a series
(Danbooru related tags and name qualifiers; e621 tag implications), so users
do not need an external list.
"""
from __future__ import annotations

import asyncio
import time

import httpx

SITE_URLS = {'danbooru': 'https://danbooru.donmai.us', 'e621': 'https://e621.net'}
PAGE_LIMIT = {'danbooru': 1000, 'e621': 320}
USER_AGENT = 'IllustrationScrappingStudio/1.1 (dataset planner tag lookup)'
CATEGORY_NAMES = {
    'danbooru': {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'meta'},
    'e621': {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'species', 6: 'invalid', 7: 'meta', 8: 'lore'},
}
# Character-category placeholders that do not name one identity (e621,
# verified 2026-10-02; Danbooru has no populated equivalents).
PLACEHOLDER_CHARACTERS = {'fan_character', 'anon', 'background_character', 'unnamed_character'}
REQUEST_INTERVAL = 1.0


class _Paced:
    """One site-safe request per second, matching the scraping providers."""

    def __init__(self):
        self.last = 0.0
        self.client = httpx.AsyncClient(timeout=60, headers={'User-Agent': USER_AGENT})

    async def get(self, url: str, params: dict):
        wait = REQUEST_INTERVAL - (time.monotonic() - self.last)
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.monotonic()
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            # e621 wraps empty results as {"tags": []} / {"tag_aliases": []}.
            data = next((value for value in data.values() if isinstance(value, list)), [])
        return data

    async def close(self):
        await self.client.aclose()


async def check_tags(family: str, tags: list[str]) -> list[dict]:
    """Classify each tag as ok, alias (with its current name), deprecated, empty or missing."""
    names = list(dict.fromkeys(tag.strip().replace(' ', '_') for tag in tags if tag.strip()))
    base = SITE_URLS[family]
    found, aliases = {}, {}
    paced = _Paced()
    try:
        for offset in range(0, len(names), 50):
            chunk = ','.join(names[offset:offset + 50])
            name_key = 'search[name_comma]' if family == 'danbooru' else 'search[name]'
            for tag in await paced.get(f'{base}/tags.json', {name_key: chunk, 'limit': 100}):
                found[tag['name']] = tag
            alias_key = 'search[antecedent_name_comma]' if family == 'danbooru' else 'search[antecedent_name]'
            for alias in await paced.get(f'{base}/tag_aliases.json', {alias_key: chunk, 'search[status]': 'active', 'limit': 100}):
                aliases[alias['antecedent_name']] = alias['consequent_name']
    finally:
        await paced.close()
    results = []
    for name in names:
        tag = found.get(name)
        item = {'tag': name, 'post_count': tag['post_count'] if tag else 0,
                'category': CATEGORY_NAMES[family].get(tag['category'], str(tag['category'])) if tag else None}
        if name in aliases:
            item.update(status='alias', replacement=aliases[name])
        elif not tag:
            item['status'] = 'missing'
        elif tag.get('is_deprecated'):
            item['status'] = 'deprecated'
        elif not tag['post_count']:
            item['status'] = 'empty'
        else:
            item['status'] = 'ok'
        results.append(item)
    return results


async def top_characters(family: str, count: int) -> list[dict]:
    """Most-posted current character tags, excluding placeholders."""
    base, limit = SITE_URLS[family], PAGE_LIMIT[family]
    params = {'search[category]': 4, 'search[order]': 'count', 'limit': limit}
    if family == 'danbooru':
        params['search[is_deprecated]'] = 'false'
    rows, page = [], 1
    paced = _Paced()
    try:
        while len(rows) < count:
            batch = await paced.get(f'{base}/tags.json', {**params, 'page': page})
            if not batch:
                break
            rows.extend({'tag': t['name'], 'post_count': t['post_count']} for t in batch
                        if t['name'] not in PLACEHOLDER_CHARACTERS and t['post_count'])
            page += 1
    finally:
        await paced.close()
    return rows[:count]


MIN_OVERLAP = 0.5  # share of a character's Danbooru posts that are in the series
IMPLICATION_DEPTH = 4


async def _danbooru_series(paced: _Paced, series: str) -> dict[str, int]:
    """Related characters (by overlap) plus every `<name>_(<series>)` tag.

    For `<base>_(series)` tags, `<name>_(<base>)` is searched too.

    Danbooru character tags rarely imply their series, and many series do not
    use a qualifier (touhou, hololive), so both sources are combined. The
    related-tag sample returns at most 500 tags.
    """
    base = SITE_URLS['danbooru']
    found = {}
    related = await paced.get(f'{base}/related_tag.json', {
        'search[query]': series, 'search[category]': 'character', 'search[order]': 'Overlap', 'limit': 1000})
    for item in related:
        tag = item.get('tag') or {}
        if tag.get('category') == 4 and not tag.get('is_deprecated') and item.get('overlap_coefficient', 0) >= MIN_OVERLAP:
            found[tag['name']] = tag['post_count']
    # fate_(series) characters are qualified "(fate)", sonic_(series) "(sonic)".
    qualifiers = [series] + ([series.removesuffix('_(series)')] if series.endswith('_(series)') else [])
    for qualifier in qualifiers:
        params = {'search[category]': 4, 'search[order]': 'count', 'search[name_matches]': f'*_({qualifier})', 'limit': PAGE_LIMIT['danbooru']}
        page = 1
        while True:
            batch = await paced.get(f'{base}/tags.json', {**params, 'page': page})
            found.update({t['name']: t['post_count'] for t in batch if not t.get('is_deprecated')})
            if len(batch) < PAGE_LIMIT['danbooru']:
                break
            page += 1
    return found


async def _e621_series(paced: _Paced, series: str) -> dict[str, int]:
    """Characters whose tags imply the series, following sub-series implications.

    e621 implies characters to their series (twilight_sparkle_(mlp) ->
    friendship_is_magic -> my_little_pony); species and general tags that also
    imply the series are skipped by category.
    """
    base = SITE_URLS['e621']
    found, seen, frontier = {}, {series}, [series]
    for _ in range(IMPLICATION_DEPTH):
        antecedents = []
        for consequent in frontier:
            page = 1
            while True:
                batch = await paced.get(f'{base}/tag_implications.json', {
                    'search[consequent_name]': consequent, 'search[status]': 'active', 'limit': PAGE_LIMIT['e621'], 'page': page})
                antecedents += [item['antecedent_name'] for item in batch if item['antecedent_name'] not in seen]
                if len(batch) < PAGE_LIMIT['e621']:
                    break
                page += 1
        antecedents = list(dict.fromkeys(antecedents))
        seen.update(antecedents)
        frontier = []
        for offset in range(0, len(antecedents), 50):
            for tag in await paced.get(f'{base}/tags.json', {'search[name]': ','.join(antecedents[offset:offset + 50]), 'limit': 100}):
                if tag['category'] == 4:
                    found[tag['name']] = tag['post_count']
                elif tag['category'] == 3:
                    frontier.append(tag['name'])
        if not frontier:
            break
    return found


async def series_characters(family: str, copyright_tag: str) -> list[dict]:
    """Every character of a series (copyright) on Danbooru or e621, most-posted first."""
    series = copyright_tag.strip().replace(' ', '_')
    if not series or any(c in series for c in '*,'):
        raise ValueError('Give one series tag, for example blue_archive')
    paced = _Paced()
    try:
        found = await (_danbooru_series if family == 'danbooru' else _e621_series)(paced, series)
    finally:
        await paced.close()
    rows = [{'tag': tag, 'post_count': count} for tag, count in found.items()
            if count and tag not in PLACEHOLDER_CHARACTERS]
    return sorted(rows, key=lambda row: (-row['post_count'], row['tag']))
