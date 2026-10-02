"""Live tag lookups for the Dataset Planner.

Tag vocabularies change: tags are renamed (aliased), deprecated or removed.
`check_tags` reports the current state of a tag list on Danbooru (also used by
Gelbooru) or e621. `top_characters` builds character targets from the sites'
own post counts, and `series_characters` lists every character of a series,
so users do not need an external list.
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


async def series_characters(family: str, copyright_tag: str) -> list[dict]:
    """Every character tag named `<character>_(<series>)`, the convention both sites use.

    Characters whose tag lacks the series qualifier are not found this way and
    can be added through a character CSV.
    """
    series = copyright_tag.strip().replace(' ', '_')
    if not series or any(c in series for c in '*,'):
        raise ValueError('Give one series tag, for example blue_archive')
    base, limit = SITE_URLS[family], PAGE_LIMIT[family]
    params = {'search[category]': 4, 'search[order]': 'count', 'search[name_matches]': f'*_({series})', 'limit': limit}
    rows, page = [], 1
    paced = _Paced()
    try:
        while True:
            batch = await paced.get(f'{base}/tags.json', {**params, 'page': page})
            rows.extend({'tag': t['name'], 'post_count': t['post_count']} for t in batch
                        if t['post_count'] and not t.get('is_deprecated'))
            if len(batch) < limit:
                break
            page += 1
    finally:
        await paced.close()
    return rows
