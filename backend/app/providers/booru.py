import httpx
import asyncio
import json
import os
import time
import xml.etree.ElementTree as ET
from typing import Callable, List, Tuple, Optional, Dict, Any
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.providers.base import Provider
from app.models import RemotePost
from app.services.provider_settings import read_provider_settings, update_provider_settings


class ProviderAuthenticationError(RuntimeError):
    """Raised when a provider requires credentials that are not configured."""


def _stored_provider_settings() -> Dict[str, Any]:
    return read_provider_settings()


def get_gelbooru_credentials() -> tuple[str, str]:
    stored = _stored_provider_settings().get('gelbooru', {})
    user_id = os.getenv('GELBOORU_USER_ID', '').strip() or str(stored.get('user_id', '')).strip()
    api_key = os.getenv('GELBOORU_API_KEY', '').strip() or str(stored.get('api_key', '')).strip()
    return user_id, api_key


def save_gelbooru_credentials(user_id: str, api_key: str) -> None:
    update_provider_settings('gelbooru', {'user_id': user_id.strip(), 'api_key': api_key.strip()})


# Booru site configurations
BOORU_SITES = {
    'danbooru': {
        'base_url': 'https://danbooru.donmai.us',
        'api_path': '/posts.json',
        'page_param': 'page',
        'tag_param': 'tags',
        'limit_param': 'limit',
        'max_limit': 200,
        'tag_categories': {
            'tag_string_artist': 'artist',
            'tag_string_character': 'character',
            'tag_string_copyright': 'copyright',
            'tag_string_general': 'general',
            'tag_string_meta': 'meta',
        },
        'image_url_field': 'file_url',
        'requires_auth': False,
        'rate_limit': 1.0,
    },
    'gelbooru': {
        'base_url': 'https://gelbooru.com',
        'api_path': '/index.php',
        'page_param': 'pid',
        'tag_param': 'tags',
        'limit_param': 'limit',
        'extra_params': {'page': 'dapi', 's': 'post', 'q': 'index', 'json': '1'},
        'max_limit': 100,
        'tag_categories': None,
        'image_url_field': 'file_url',
        'requires_auth': True,
        'rate_limit': 1.0,
    },
    'e621': {
        'base_url': 'https://e621.net',
        'api_path': '/posts.json',
        'page_param': 'page',
        'tag_param': 'tags',
        'limit_param': 'limit',
        'max_limit': 320,
        'requires_user_agent': True,
        'tag_categories': {
            'artist': 'artist',
            'character': 'character',
            'copyright': 'copyright',
            'species': 'species',
            'general': 'general',
            'meta': 'meta',
        },
        'rate_limit': 1.0,
    },
}

GELBOORU_TAG_TYPES = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "meta",
}


class BooruProvider(Provider):
    """Generic booru provider supporting Danbooru, Gelbooru, e621."""

    def __init__(self, site: str):
        if site not in BOORU_SITES:
            raise ValueError(f"Unsupported booru site: {site}. Supported: {list(BOORU_SITES.keys())}")

        self.site = site
        # Gelbooru's `pid` is a moving page offset, unlike Danbooru/e621's
        # stable post-ID keyset. It needs the ordered-feed sync strategy.
        self.ordered_feed = site == 'gelbooru'
        self.config = BOORU_SITES[site]
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                'User-Agent': 'ArtistCollectionBuilder/1.0 (https://github.com/artist-collection)',
                'Accept': 'application/json, application/xml;q=0.9, text/xml;q=0.8',
            }
        )
        self.rate_limiter = asyncio.Semaphore(1)
        self._last_request_at = 0.0

        # Set specific User-Agent for e621
        if self.config.get('requires_user_agent'):
            self.client.headers['User-Agent'] = 'ArtistCollectionBuilder/1.0 (contact@example.com)'
        if self.site == 'gelbooru':
            # Gelbooru's DAPI requires both values for post search and lookup.
            # Keep secrets server-side and never return them in API responses.
            self.gelbooru_user_id, self.gelbooru_api_key = get_gelbooru_credentials()
            self.client.headers['Referer'] = 'https://gelbooru.com/'
        else:
            self.gelbooru_user_id = ''
            self.gelbooru_api_key = ''
        self._tag_category_cache: Dict[str, str] = {}

    @property
    def credentials_configured(self) -> bool:
        return self.site != 'gelbooru' or bool(self.gelbooru_user_id and self.gelbooru_api_key)

    def _require_credentials(self) -> None:
        if self.site == 'gelbooru' and not self.credentials_configured:
            raise ProviderAuthenticationError(
                'Gelbooru requires a User ID and API key. Configure them in Settings before syncing.'
            )

    def _gelbooru_params(self) -> Dict[str, str]:
        self._require_credentials()
        return {'user_id': self.gelbooru_user_id, 'api_key': self.gelbooru_api_key} if self.site == 'gelbooru' else {}

    def _raise_for_status(self, response: httpx.Response, operation: str) -> None:
        if self.site == 'gelbooru' and response.status_code in {401, 403}:
            raise ProviderAuthenticationError(
                f'Gelbooru rejected the configured credentials while trying to {operation}. '
                'Update the User ID and API key in Settings.'
            )
        if self.site == 'gelbooru' and response.is_error:
            # httpx includes the complete request URL in its default exception;
            # Gelbooru credentials are query parameters, so never expose it.
            raise RuntimeError(f'Gelbooru returned HTTP {response.status_code} while trying to {operation}')
        response.raise_for_status()

    @staticmethod
    def _response_posts(response: httpx.Response) -> list[Dict[str, Any]]:
        """Decode JSON and XML post lists exposed by Gelbooru."""
        content_type = response.headers.get('content-type', '').lower()
        try:
            data = response.json()
            if isinstance(data, dict):
                data = data.get('posts', data.get('post', []))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            if 'xml' not in content_type and not response.text.lstrip().startswith('<'):
                raise ValueError('Gelbooru returned an unsupported response format')
            root = ET.fromstring(response.text)
            return [dict(item.attrib) for item in root.findall('.//post')]

    @staticmethod
    def _response_tags(response: httpx.Response) -> list[Dict[str, Any]]:
        """Decode Gelbooru's JSON or XML tag-index response."""
        content_type = response.headers.get('content-type', '').lower()
        try:
            data = response.json()
            if isinstance(data, dict):
                data = data.get('tag', data.get('tags', []))
            if isinstance(data, dict):
                return [data]
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            if 'xml' not in content_type and not response.text.lstrip().startswith('<'):
                raise ValueError('Gelbooru returned an unsupported tag response format')
            root = ET.fromstring(response.text)
            return [dict(item.attrib) for item in root.findall('.//tag')]

    async def _gelbooru_tag_categories(self, tag_names: List[str]) -> Dict[str, str]:
        """Resolve Gelbooru's flat post tags through its batched tag index."""
        unique = list(dict.fromkeys(str(tag) for tag in tag_names if str(tag)))
        missing = [tag for tag in unique if tag not in self._tag_category_cache]
        url = self.config['base_url'] + self.config['api_path']
        for offset in range(0, len(missing), 100):
            batch = missing[offset:offset + 100]
            await self._rate_limit()
            params = {
                'page': 'dapi', 's': 'tag', 'q': 'index', 'json': '1',
                'limit': '100', 'names': ' '.join(batch),
                **self._gelbooru_params(),
            }
            response = await self.client.get(url, params=params)
            self._raise_for_status(response, 'look up tag categories')
            resolved = {}
            for item in self._response_tags(response):
                name = str(item.get('name', '')).strip()
                try:
                    tag_type = int(item.get('type', 0))
                except (TypeError, ValueError):
                    tag_type = 0
                if name:
                    resolved[name] = GELBOORU_TAG_TYPES.get(tag_type, 'general')
            for tag in batch:
                self._tag_category_cache[tag] = resolved.get(tag, 'general')
        return {tag: self._tag_category_cache.get(tag, 'general') for tag in unique}

    async def _rate_limit(self):
        """Keep the configured start-to-start interval without idle over-sleep."""
        interval = float(self.config['rate_limit'])
        elapsed = time.monotonic() - self._last_request_at
        remaining = interval - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _parse_tags(
        self,
        post_data: Dict[str, Any],
        category_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, List[str]]:
        """Parse tags from post data into categorized dict."""
        tags = {}

        if self.site == 'danbooru':
            # Danbooru has tag_string_* fields
            for field, category in self.config['tag_categories'].items():
                tag_string = post_data.get(field, '')
                if tag_string:
                    tags[category] = tag_string.split()

        elif self.site == 'e621':
            # e621 has nested tags object
            tag_obj = post_data.get('tags', {})
            for field, category in self.config['tag_categories'].items():
                tag_list = tag_obj.get(field, [])
                if tag_list:
                    tags[category] = tag_list

        elif self.site == 'gelbooru':
            # Post payloads are flat; the tag-index API supplies their types.
            tag_string = post_data.get('tags', '')
            if tag_string:
                for tag in tag_string.split():
                    category = (category_map or {}).get(tag, 'general')
                    tags.setdefault(category, []).append(tag)

        return tags

    def _post_to_remote(
        self,
        post_data: Dict[str, Any],
        category_map: Optional[Dict[str, str]] = None,
    ) -> RemotePost:
        """Convert API post data to RemotePost model."""
        file_data = post_data.get('file', {}) if self.site == 'e621' else {}
        image_url = file_data.get('url', '') if self.site == 'e621' else post_data.get(self.config['image_url_field'], '')
        preview_url = None
        if self.site == 'e621':
            preview_url = (post_data.get('preview') or {}).get('url')
            image_url = image_url or (post_data.get('sample') or {}).get('url') or preview_url or ''
        else:
            preview_url = post_data.get('preview_file_url') or post_data.get('preview_url') or post_data.get('sample_url')
            image_url = image_url or post_data.get('sample_url') or post_data.get('preview_file_url') or ''

        # Handle relative URLs
        if image_url and not image_url.startswith('http'):
            image_url = self.config['base_url'] + image_url
        if preview_url and not preview_url.startswith('http'):
            preview_url = self.config['base_url'] + preview_url

        # Get dimensions
        width = file_data.get('width', 0) if self.site == 'e621' else post_data.get('width', post_data.get('image_width', 0))
        height = file_data.get('height', 0) if self.site == 'e621' else post_data.get('height', post_data.get('image_height', 0))

        # Determine format from file extension
        file_ext = file_data.get('ext', '') if self.site == 'e621' else post_data.get('file_ext', '')
        if not file_ext and image_url:
            file_ext = Path(urlparse(image_url).path).suffix.lstrip('.')

        # Build remote URL
        post_id = str(post_data.get('id', ''))
        if self.site == 'danbooru':
            remote_url = f"{self.config['base_url']}/posts/{post_id}"
        elif self.site == 'gelbooru':
            remote_url = f"{self.config['base_url']}/index.php?page=post&s=view&id={post_id}"
        elif self.site == 'e621':
            remote_url = f"{self.config['base_url']}/posts/{post_id}"
        else:
            remote_url = image_url

        rating = post_data.get('rating')
        rating = {'s': 'safe', 'q': 'questionable', 'e': 'explicit'}.get(rating, rating)
        score = post_data.get('score')
        if isinstance(score, dict):
            score = score.get('total')
        source = post_data.get('source')
        if not source and isinstance(post_data.get('sources'), list):
            source = post_data['sources'][0] if post_data['sources'] else None

        created_at = post_data.get('created_at', datetime.now(timezone.utc).isoformat())
        if not isinstance(created_at, str):
            created_at = created_at.isoformat() if hasattr(created_at, 'isoformat') else str(created_at)

        return RemotePost(
            provider=self.site,
            remote_id=post_id,
            remote_url=remote_url,
            image_url=image_url,
            preview_url=preview_url or image_url,
            width=width,
            height=height,
            format=file_ext,
            md5=file_data.get('md5') if self.site == 'e621' else post_data.get('md5', post_data.get('hash')),
            tags=self._parse_tags(post_data, category_map),
            rating=rating,
            score=score,
            source=source,
            parent_id=str(post_data.get('parent_id')) if post_data.get('parent_id') else None,
            created_at=created_at,
            raw_metadata=post_data
        )

    async def search(
        self,
        query: str,
        cursor: Optional[str] = None,
        limit: int = 100,
        sort: str = "latest",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Tuple[List[RemotePost], Optional[str]]:
        """Search posts matching query."""
        async with self.rate_limiter:
            await self._rate_limit()

            # Build query parameters
            params = {}

            # Add extra params (for Gelbooru)
            if 'extra_params' in self.config:
                params.update(self.config['extra_params'])
            params.update(self._gelbooru_params())

            # Booru APIs expose ordering/date constraints as query tags. Keep
            # the UI provider-neutral while translating them here.
            query_parts = query.split() if getattr(self, 'literal_query', False) else self._normalize_query(query)
            if self.site == 'gelbooru':
                # These providers search canonical tag names directly and do
                # not understand Danbooru's artist:/character: namespaces.
                query_parts = [part.split(':', 1)[1] if part.startswith(('artist:', 'character:', 'copyright:')) else part for part in query_parts]
            # These metatags are supported by Danbooru/e621. Gelbooru uses
            # a different query dialect, so do not send them
            # provider-blindly (which turns a valid search into zero results).
            if self.site in {'danbooru', 'e621'}:
                query_parts.append("order:id_asc" if sort == "oldest" else "order:id_desc")
                if date_from:
                    query_parts.append(f"created_at:>={date_from}")
                if date_to:
                    query_parts.append(f"created_at:<={date_to}")
            params[self.config['tag_param']] = " ".join(query_parts)

            # Add limit
            if 'limit_param' in self.config:
                params[self.config['limit_param']] = (
                    self.config['max_limit'] if self.site == 'gelbooru'
                    else min(limit, self.config['max_limit'])
                )

            if self.site == 'gelbooru':
                params['sort'] = 'id:asc' if sort == 'oldest' else 'id:desc'

            # Add cursor/pagination
            if cursor:
                if self.site in ['danbooru', 'e621']:
                    # Keyset pagination: latest results continue toward older
                    # IDs (after), while oldest results continue toward newer.
                    params[self.config['page_param']] = f"{'a' if sort == 'oldest' else 'b'}{cursor}"
                elif self.site != 'gelbooru':
                    # Offset pagination
                    params[self.config['page_param']] = cursor

            # Make request
            url = self.config['base_url'] + self.config['api_path']
            if self.site == 'gelbooru':
                # DAPI pid is a *page* offset and its meaning changes with
                # `limit`. Use a fixed page size and expose an absolute-item
                # cursor so Sync All and folder sync can request any slice.
                page_size = self.config['max_limit']
                absolute_offset = int(cursor[2:]) if cursor and cursor.startswith('o:') else 0
                wanted = min(limit, self.config['max_limit'])
                posts_data = []
                position = absolute_offset
                while len(posts_data) < wanted:
                    page_params = {**params, self.config['page_param']: position // page_size}
                    response = await self.client.get(url, params=page_params)
                    self._raise_for_status(response, 'search posts')
                    page_posts = self._response_posts(response)
                    local_offset = position % page_size
                    selected = page_posts[local_offset:local_offset + wanted - len(posts_data)]
                    posts_data.extend(selected)
                    position += len(selected)
                    if len(page_posts) < page_size or not selected:
                        break
                    if len(posts_data) < wanted:
                        await self._rate_limit()
            else:
                response = await self.client.get(url, params=params)
                self._raise_for_status(response, 'search posts')
                data = response.json()
                # e621 uses `posts`; other adapters generally return a list.
                posts_data = data.get('posts', data.get('post', [])) if isinstance(data, dict) else data

            # Gelbooru post payloads flatten every tag. Resolve the complete
            # page in batches so character/copyright/general ordering survives
            # ingest without issuing one request per tag.
            category_map = None
            if self.site == 'gelbooru':
                page_tags = [
                    tag
                    for post_data in posts_data
                    for tag in str(post_data.get('tags', '')).split()
                ]
                category_map = await self._gelbooru_tag_categories(page_tags)

            # Convert to RemotePost objects
            normalized_posts = [
                self._post_to_remote(p, category_map) for p in posts_data if p.get('id')
            ]
            posts = normalized_posts

            # Some adapters do not expose a portable date query. Apply the
            # requested range after normalization as a second line of defense.
            if date_from or date_to:
                def in_date_range(post: RemotePost) -> bool:
                    value = (post.created_at or '').replace('Z', '+00:00')
                    try:
                        created = datetime.fromisoformat(value).date()
                    except (TypeError, ValueError):
                        return True
                    return (not date_from or created >= datetime.fromisoformat(date_from).date()) and (not date_to or created <= datetime.fromisoformat(date_to).date())
                posts = [post for post in posts if in_date_range(post)]

            # Determine next cursor
            next_cursor = None
            page_size = min(limit, self.config['max_limit'])
            if normalized_posts and len(posts_data) >= page_size:
                if self.site in ['danbooru', 'e621']:
                    # Use last post ID as cursor
                    next_cursor = normalized_posts[-1].remote_id
                else:
                    next_cursor = f"o:{absolute_offset + len(posts_data)}"

            return posts, next_cursor

    @staticmethod
    def _normalize_query(query: str) -> List[str]:
        """Accept human-readable artist/character names without underscores."""
        # Keep canonical booru underscores when the user supplies them. For
        # spaced artist/character names, the operator-specific branch below
        # joins the words into the canonical tag form.
        tokens = query.split()
        parts: List[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith(("artist:", "character:", "copyright:")):
                prefix, value = token.split(":", 1)
                values = [value]
                index += 1
                while index < len(tokens) and ":" not in tokens[index] and not tokens[index].startswith("-"):
                    values.append(tokens[index])
                    index += 1
                parts.append(f"{prefix}:{'_'.join(values)}")
                continue
            parts.append(token)
            index += 1
        return parts

    async def download_image(
        self,
        post: RemotePost,
        dest_path: str,
        progress: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """Download image to destination path."""
        # Rate-limit request starts, not the entire response body. Worker
        # concurrency may overlap transfers that have already been admitted.
        if progress:
            progress({"stage": "waiting_for_provider", "remote_id": post.remote_id,
                      "format": post.format, "request_interval_seconds": float(self.config['rate_limit'])})
        async with self.rate_limiter:
            await self._rate_limit()
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        downloaded = 0
        last_reported = started

        def report(total: int | None, complete: bool = False) -> None:
            if not progress:
                return
            elapsed = max(time.monotonic() - started, 0.001)
            speed = downloaded / elapsed
            eta = ((total - downloaded) / speed) if total and speed > 0 and downloaded < total else 0 if complete else None
            progress({
                "stage": "complete" if complete else "downloading",
                "remote_id": post.remote_id,
                "format": post.format,
                "bytes_downloaded": downloaded,
                "bytes_total": total,
                "percent": min(100, round(downloaded * 100 / total, 1)) if total else None,
                "speed_bps": speed,
                "eta_seconds": eta,
                "elapsed_seconds": elapsed,
            })

        async with self.client.stream("GET", post.image_url) as response:
            self._raise_for_status(response, 'download an image')
            try:
                total = int(response.headers.get("content-length", "")) or None
            except ValueError:
                total = None
            report(total)
            with open(dest_path, 'wb') as destination:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    destination.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_reported >= 0.25:
                        report(total)
                        last_reported = now
            report(total or downloaded, complete=True)

    async def get_post(self, remote_id: str) -> RemotePost:
        """Fetch metadata for a single post."""
        async with self.rate_limiter:
            await self._rate_limit()

            # Build URL
            if self.site == 'danbooru':
                url = f"{self.config['base_url']}/posts/{remote_id}.json"
                response = await self.client.get(url)
            elif self.site == 'e621':
                url = f"{self.config['base_url']}/posts/{remote_id}.json"
                response = await self.client.get(url)
            else:
                # For others, search by ID
                params = {self.config['tag_param']: f"id:{remote_id}"}
                if 'extra_params' in self.config:
                    params.update(self.config['extra_params'])
                params.update(self._gelbooru_params())
                url = self.config['base_url'] + self.config['api_path']
                response = await self.client.get(url, params=params)

            self._raise_for_status(response, 'look up a post')
            if self.site == 'gelbooru':
                posts = self._response_posts(response)
                if not posts:
                    raise ValueError(f'Gelbooru returned no post for ID {remote_id}')
                post_data = posts[0]
            else:
                data = response.json()
                if isinstance(data, dict) and 'post' in data:
                    post_data = data['post']
                    if isinstance(post_data, list):
                        post_data = post_data[0]
                elif isinstance(data, list):
                    post_data = data[0]
                else:
                    post_data = data

            category_map = None
            if self.site == 'gelbooru':
                category_map = await self._gelbooru_tag_categories(
                    str(post_data.get('tags', '')).split()
                )
            return self._post_to_remote(post_data, category_map)

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
