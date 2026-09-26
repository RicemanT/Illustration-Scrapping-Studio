"""gallery-dl backed providers for sites without a stable public post API."""

import asyncio
import difflib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse, urlunparse

import httpx
from curl_cffi import requests as curl_requests

from app.models import RemotePost
from app.db import LIBRARY_PATH
from app.providers.base import Provider
from app.providers.booru import ProviderAuthenticationError
from app.services.provider_settings import read_provider_settings, update_provider_settings
from app.services.settings import get_parallel_workers
from app.services.workspace import scratch_directory


GALLERY_DL_PROVIDERS = {
    "pixiv": {"requires_auth": True, "max_limit": 200},
    "artstation": {"requires_auth": False, "max_limit": 200},
    "twitter": {"requires_auth": True, "max_limit": 200},
    "pawchive": {"requires_auth": False, "max_limit": 200},
}
TWITTER_BROWSERS = {"firefox", "chrome", "chromium", "edge", "brave", "opera", "vivaldi", "safari"}
TWITTER_COOKIE_PATH = LIBRARY_PATH / ".secrets" / "twitter.cookies.txt"
ARTSTATION_SIZE_SEGMENTS = ("small", "medium", "large", "4k", "8k", "original")
ARTSTATION_COVER_SIZE_SEGMENTS = (*ARTSTATION_SIZE_SEGMENTS, "smaller_square")
ARTSTATION_IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif")
# gallery-dl applies file filters before file ranges. Filtering here means an
# Amount of 10 remains ten ArtStation projects rather than ten arbitrary
# process assets from the first few projects.
ARTSTATION_COVER_FILTER = (
    "cover_url and asset and asset.get('image_url') and "
    "cover_url.split('/p/assets/', 1)[-1].split('?', 1)[0]"
    ".replace('/small/', '/').replace('/medium/', '/').replace('/large/', '/')"
    ".replace('/4k/', '/').replace('/8k/', '/').replace('/original/', '/') == "
    "asset['image_url'].split('/p/assets/', 1)[-1].split('?', 1)[0]"
    ".replace('/small/', '/').replace('/medium/', '/').replace('/large/', '/')"
    ".replace('/4k/', '/').replace('/8k/', '/').replace('/original/', '/') and "
    "(not asset.get('has_embedded_player') or "
    "('/assets/marmosets/' in asset.get('image_url', '') and extension in "
    f"{ARTSTATION_IMAGE_EXTENSIONS!r}) or "
    "('artstation.com' not in asset.get('player_embedded', '') and extension in "
    f"{ARTSTATION_IMAGE_EXTENSIONS!r}) or "
    "('/assets/marmosets/' not in asset.get('image_url', '') and "
    "'artstation.com' in asset.get('player_embedded', '') and extension not in "
    f"{ARTSTATION_IMAGE_EXTENSIONS!r}))"
)
def gallery_dl_runtime() -> dict[str, Any]:
    installed = importlib.util.find_spec("gallery_dl") is not None
    try:
        installed_version = version("gallery-dl") if installed else None
    except PackageNotFoundError:
        installed_version = None
    return {"installed": installed, "version": installed_version}


def get_gallery_dl_settings() -> dict[str, Any]:
    stored = read_provider_settings()
    twitter = stored.get("twitter") or {}
    browser = str(twitter.get("browser", "")).strip().lower()
    profile = str(twitter.get("profile", "")).strip()
    configured_cookie_path = Path(os.getenv(
        "TWITTER_COOKIES_FILE", str(TWITTER_COOKIE_PATH)
    )).expanduser().resolve()
    cookie_file = str(configured_cookie_path) if configured_cookie_path.is_file() else ""
    auth_mode = str(twitter.get("auth_mode", "")).strip().lower()
    if os.getenv("TWITTER_COOKIES_FILE", "").strip():
        auth_mode = "cookies_file"
    elif auth_mode not in {"browser", "cookies_file"}:
        auth_mode = "browser" if browser else ("cookies_file" if cookie_file else "")
    return {
        "pixiv_refresh_token": os.getenv("PIXIV_REFRESH_TOKEN", "").strip()
        or str((stored.get("pixiv") or {}).get("refresh_token", "")).strip(),
        "twitter_auth_mode": auth_mode,
        "twitter_browser": browser,
        "twitter_profile": profile,
        "twitter_cookie_file": cookie_file,
    }


def save_pixiv_refresh_token(refresh_token: str) -> None:
    update_provider_settings("pixiv", {"refresh_token": refresh_token.strip()})


def save_twitter_browser(browser: str, profile: str = "") -> None:
    update_provider_settings("twitter", {
        "auth_mode": "browser",
        "browser": browser.strip().lower(),
        "profile": profile.strip(),
    })


def save_twitter_cookie_file(contents: str) -> dict[str, Any]:
    """Validate and store a Netscape cookie export without returning secrets."""
    from gallery_dl.util import cookiestxt_load

    try:
        cookies = cookiestxt_load(StringIO(contents))
    except Exception as exc:
        raise ValueError("Invalid Netscape cookies.txt file") from exc
    twitter_cookies = [
        cookie for cookie in cookies
        if cookie.domain.lstrip(".").lower() in {"x.com", "twitter.com"}
    ]
    names = {cookie.name for cookie in twitter_cookies if cookie.value}
    missing = {"auth_token", "ct0"} - names
    if missing:
        raise ValueError(
            "The export must include signed-in X cookies for x.com: "
            + ", ".join(sorted(missing))
        )

    TWITTER_COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = TWITTER_COOKIE_PATH.with_name(f".{TWITTER_COOKIE_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(contents, encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(TWITTER_COOKIE_PATH)
    finally:
        temporary.unlink(missing_ok=True)
    current = read_provider_settings().get("twitter") or {}
    update_provider_settings("twitter", {
        "auth_mode": "cookies_file",
        "browser": str(current.get("browser", "")).strip().lower(),
        "profile": str(current.get("profile", "")).strip(),
    })
    return {"configured": True, "auth_mode": "cookies_file", "cookie_count": len(twitter_cookies)}


def remove_twitter_cookie_file() -> dict[str, Any]:
    TWITTER_COOKIE_PATH.unlink(missing_ok=True)
    current = read_provider_settings().get("twitter") or {}
    browser = str(current.get("browser", "")).strip().lower()
    profile = str(current.get("profile", "")).strip()
    update_provider_settings("twitter", {
        "auth_mode": "browser" if browser else "",
        "browser": browser,
        "profile": profile,
    })
    return {"configured": bool(browser), "auth_mode": "browser" if browser else ""}


def provider_ready(name: str) -> tuple[bool, Optional[str]]:
    runtime = gallery_dl_runtime()
    if not runtime["installed"]:
        return False, "gallery-dl is not installed"
    settings = get_gallery_dl_settings()
    if name == "pixiv" and not settings["pixiv_refresh_token"]:
        return False, "Pixiv OAuth refresh token is not configured"
    if name == "twitter":
        mode = settings["twitter_auth_mode"]
        if mode == "cookies_file" and settings["twitter_cookie_file"]:
            return True, None
        if mode == "browser" and settings["twitter_browser"]:
            return True, None
        return False, "Twitter/X cookies are not configured"
    return True, None


def provider_max_limit(name: str) -> int:
    return int(GALLERY_DL_PROVIDERS.get(name, {}).get("max_limit", 100))


def _public_metadata(value: Any) -> Any:
    """Remove gallery-dl private transport data and anything credential-like."""
    if isinstance(value, dict):
        return {
            str(key): _public_metadata(item)
            for key, item in value.items()
            if not str(key).startswith("_")
            and not any(word in str(key).lower() for word in ("cookie", "token", "authorization", "password"))
        }
    if isinstance(value, list):
        return [_public_metadata(item) for item in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", " ").split() if item.strip()]
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("tag")
            if item:
                result.append(str(item).strip())
        return result
    return []


def _nested(data: dict, *paths: tuple[str, ...], default=None):
    for path in paths:
        value: Any = data
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, ""):
            return value
    return default


def _artstation_asset_identity(url: Any) -> str:
    """Return the size-independent CDN path identifying one ArtStation asset."""
    if not isinstance(url, str) or not url:
        return ""
    path = urlparse(url).path.casefold()
    marker = "/p/assets/"
    if marker in path:
        path = path.split(marker, 1)[1]
    parts = [part for part in path.split("/") if part]
    return "/".join(part for part in parts if part not in ARTSTATION_SIZE_SEGMENTS)


def _artstation_cover_tier(url: str, tier: str) -> str:
    """Replace an ArtStation CDN thumbnail tier without changing its asset."""
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    for index, part in enumerate(parts):
        if part.casefold() in ARTSTATION_COVER_SIZE_SEGMENTS:
            parts[index] = tier
            break
    return urlunparse(parsed._replace(path="/".join(parts)))


def _artstation_slug(query: str) -> str:
    value = query.strip()
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = (parsed.hostname or "").casefold()
        if host.endswith(".artstation.com") and host not in {"artstation.com", "www.artstation.com"}:
            return host.removesuffix(".artstation.com")
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].casefold() not in {"artwork", "projects", "search"}:
            return parts[0]
        return ""
    if value.casefold().startswith("artist:"):
        value = value.split(":", 1)[1]
    value = " ".join(token for token in value.split() if not token.startswith("-"))
    return value.lstrip("@").replace(" ", "-")


class _ArtstationPortfolioParser(HTMLParser):
    """Read project IDs and cover assets from an artist's public portfolio."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.projects: list[dict[str, str]] = []
        self._project: dict[str, str] | None = None
        self._capture_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a":
            match = re.fullmatch(r"/projects/([A-Za-z0-9]+)", attributes.get("href") or "")
            if match:
                self._project = {"hash_id": match.group(1), "cover_url": "", "title": ""}
        elif tag == "img" and self._project is not None and not self._project["cover_url"]:
            self._project["cover_url"] = attributes.get("src") or ""
        elif tag == "div" and self._project is not None:
            classes = (attributes.get("class") or "").split()
            if "album-grid-item-name" in classes:
                self._capture_title = True

    def handle_data(self, data: str) -> None:
        if self._capture_title and self._project is not None:
            self._project["title"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._capture_title:
            self._capture_title = False
        elif tag == "a" and self._project is not None:
            if self._project["cover_url"]:
                self._project["title"] = self._project["title"].strip()
                self.projects.append(self._project)
            self._project = None


def _parse_artstation_portfolio(html: str) -> list[dict[str, str]]:
    parser = _ArtstationPortfolioParser()
    parser.feed(html)
    seen: set[str] = set()
    return [
        project for project in parser.projects
        if not (project["hash_id"] in seen or seen.add(project["hash_id"]))
    ]


class _ArtstationProjectParser(HTMLParser):
    """Extract real artwork/video assets from an artist-site project page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.videos: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and "colorbox-gal" in (attributes.get("class") or "").split():
            url = attributes.get("href") or ""
            if url.startswith(("http://", "https://")) and url not in self.images:
                self.images.append(url)
        elif tag in {"source", "video"}:
            url = attributes.get("src") or ""
            extension = Path(urlparse(url).path).suffix.casefold()
            if extension in {".mp4", ".webm", ".mov", ".m4v"} and url not in self.videos:
                self.videos.append(url)


def _parse_artstation_project(html: str) -> tuple[list[str], list[str]]:
    parser = _ArtstationProjectParser()
    parser.feed(html)
    return parser.images, parser.videos


def _artstation_original_tiers(url: str) -> list[str]:
    tiers: list[str] = []
    for tier in ("8k", "4k", "large", "medium", "small"):
        candidate = _artstation_cover_tier(url, tier)
        if candidate not in tiers:
            tiers.append(candidate)
    if url not in tiers:
        tiers.append(url)
    return tiers


def _artstation_asset_id(url: str) -> str:
    match = re.search(r"/images/images/(\d+)/(\d+)/(\d+)/", urlparse(url).path)
    if match:
        return "".join(match.groups()).lstrip("0") or "0"
    return _artstation_asset_identity(url).replace("/", "-")


def _artstation_filename_similarity(cover_url: str, asset_url: str) -> float:
    def normalized(url: str) -> str:
        stem = Path(urlparse(url).path).stem.casefold()
        for noise in ("thumbnail", "thumb", "cover", "smaller", "mini", "jpeg", "jpg", "png"):
            stem = stem.replace(noise, "")
        return re.sub(r"[^a-z0-9]+", "", stem)

    return difflib.SequenceMatcher(None, normalized(cover_url), normalized(asset_url)).ratio()


def _select_artstation_cover_messages(
    messages: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Keep the original media asset represented by each project's cover."""
    projects: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for url, data in messages:
        project_id = str(data.get("hash_id") or _nested(data, ("project", "hash_id"), default=""))
        if not project_id:
            continue
        projects.setdefault(project_id, []).append((url, data))

    selected: list[tuple[str, dict[str, Any]]] = []
    for project_id, candidates in projects.items():
        matches = []
        for url, data in candidates:
            cover = _artstation_asset_identity(data.get("cover_url"))
            asset_image = _artstation_asset_identity(_nested(data, ("asset", "image_url"), default=""))
            if cover and cover == asset_image:
                matches.append((url, data))
        # A single-asset project is unambiguous even when older ArtStation
        # payloads omit cover_url. Multi-asset projects are skipped unless the
        # cover can be matched; guessing would risk importing process images.
        if matches:
            def media_rank(item: tuple[str, dict[str, Any]]) -> int:
                url, data = item
                extension = str(data.get("extension") or Path(urlparse(url).path).suffix.lstrip(".")).casefold()
                asset_image = str(_nested(data, ("asset", "image_url"), default=""))
                player = str(_nested(data, ("asset", "player_embedded"), default=""))
                is_image = extension in ARTSTATION_IMAGE_EXTENSIONS
                is_marmoset = "/assets/marmosets/" in asset_image
                is_external_video = bool(data.get("asset", {}).get("has_embedded_player")) and "artstation.com" not in player
                # Video assets retain original video for cover-guided frame
                # extraction. Interactive Marmoset viewers instead use their
                # attached still because .mview is not trainer-readable.
                # External players also use the attached original project
                # still; this avoids importing a player thumbnail.
                return 0 if ((is_marmoset or is_external_video) and is_image) or (
                    not is_marmoset and not is_external_video and not is_image
                ) else 1

            choice = min(matches, key=media_rank)
        else:
            choice = candidates[0] if len(candidates) == 1 else None
        if choice:
            url, data = choice
            data = dict(data)
            data["artstation_selection"] = {
                "method": "cover_asset_path" if matches else "single_asset",
                "project_id": project_id,
                "candidate_count": int(data.get("count") or len(candidates)),
                "selected_asset_id": _nested(data, ("asset", "id"), default=None),
            }
            selected.append((url, data))
    return selected


class GalleryDLProvider(Provider):
    """Runs gallery-dl as an isolated metadata extractor and downloads originals."""

    ordered_feed = True
    _post_cache: dict[tuple[str, str], RemotePost] = {}
    _transport_cache: dict[tuple[str, str], dict[str, Any]] = {}
    _artstation_portfolio_until: dict[str, float] = {}

    def __init__(self, site: str):
        if site not in GALLERY_DL_PROVIDERS:
            raise ValueError(f"Unsupported gallery-dl provider: {site}")
        ready, reason = provider_ready(site)
        if not ready:
            raise ProviderAuthenticationError(reason or f"{site} is not configured")
        self.site = site
        self.settings = get_gallery_dl_settings()
        self.search_progress: Callable[[dict], None] | None = None
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
            headers={"User-Agent": "ArtistCollectionBuilder/1.0"},
        )

    def _target_url(self, query: str) -> str:
        value = query.strip()
        if value.startswith(("http://", "https://")):
            return value
        if value.casefold().startswith("artist:"):
            value = value.split(":", 1)[1]
        # Folder filters are meaningful to boorus, but profile-oriented sites
        # need only the creator identity. Date constraints are applied after
        # extraction and training-format constraints during ingest.
        value = " ".join(
            token for token in value.split()
            if not token.startswith("-") and not any(token.casefold().startswith(prefix) for prefix in (
                "rating:", "character:", "copyright:", "order:", "created_at:",
            ))
        )
        if self.site == "pixiv":
            if value.isdigit():
                return f"https://www.pixiv.net/en/users/{value}/artworks"
            return f"https://www.pixiv.net/en/tags/{quote(value, safe='')}/artworks"
        if self.site == "artstation":
            slug = value.lstrip("@").replace(" ", "-")
            return f"https://www.artstation.com/{quote(slug, safe='-_.')}"
        if self.site == "twitter":
            handle = value.lstrip("@").replace(" ", "")
            return f"https://x.com/{quote(handle, safe='_')}/media"
        raise ValueError("Pawchive queries must be a full creator or post URL")

    def _config(self) -> dict[str, Any]:
        extractor: dict[str, Any] = {
            "timeout": 120,
            "retries": 2,
            "pixiv": {"ugoira": True, "tags": "original"},
            "artstation": {
                # Fail over promptly to the public portfolio transport instead
                # of repeating a connection reset inside gallery-dl.
                "retries": 0,
                "videos": True,
                "mviews": True,
                "previews": True,
                "image-filter": ARTSTATION_COVER_FILTER,
            },
            "twitter": {"size": ["orig", "4096x4096"], "include": "media", "retweets": False},
            "pawchive": {"original": True, "previews": False, "metadata": True},
        }
        if self.settings["pixiv_refresh_token"]:
            extractor["pixiv"]["refresh-token"] = self.settings["pixiv_refresh_token"]
        if self.settings.get("twitter_auth_mode") == "cookies_file" and self.settings.get("twitter_cookie_file"):
            extractor["twitter"]["cookies"] = self.settings["twitter_cookie_file"]
        elif self.settings.get("twitter_browser"):
            cookie_source: list[str] = [self.settings["twitter_browser"]]
            if self.settings["twitter_profile"]:
                cookie_source.append(self.settings["twitter_profile"])
            extractor["twitter"]["cookies"] = cookie_source
        return {"extractor": extractor, "output": {"private": True}}

    async def _run_gallery_dl(self, target: str, start: int, end: int) -> list[tuple[str, dict[str, Any]]]:
        # --config adds to default user configuration unless explicitly ignored.
        # External actions/plugins/output paths must never affect app discovery.
        with tempfile.TemporaryDirectory(prefix="provider-", dir=scratch_directory()) as workdir:
            config_path = Path(workdir) / "config.json"
            config = self._config()
            config["extractor"].update({"base-directory": workdir, "download": False})
            config["cache"] = {"file": str(Path(workdir) / "cache.sqlite3")}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({"TMP": workdir, "TEMP": workdir, "TMPDIR": workdir})
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "gallery_dl", "--config-ignore",
                "--no-download", "--resolve-json",
                "--config", str(config_path), "--range", f"{start}-{end}", "--", target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=workdir, env=environment,
            )
            try:
                stdout, stderr = await process.communicate()
            finally:
                # Cancellation must not leave a helper running after its job ends.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if process.returncode:
                message = stderr.decode("utf-8", "replace").strip().splitlines()
                detail = message[-1] if message else f"gallery-dl exited with code {process.returncode}"
                if "auth" in detail.lower() or "login" in detail.lower() or "cookie" in detail.lower():
                    raise ProviderAuthenticationError(f"{self.site}: {detail}")
                raise RuntimeError(f"{self.site}: {detail}")
            output = stdout.decode("utf-8", "replace")
            embedded_errors = self._decode_errors(output)
            if embedded_errors:
                detail = embedded_errors[-1]
                if "auth" in detail.lower() or "token" in detail.lower() or "login" in detail.lower():
                    raise ProviderAuthenticationError(f"{self.site}: {detail}")
                raise RuntimeError(f"{self.site}: {detail}")
            return self._decode_messages(output)

    async def _artstation_portfolio_fallback(
        self,
        query: str,
        offset: int,
        limit: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        """Read real project assets from the artist host when the API is blocked."""
        slug = _artstation_slug(query)
        if not slug:
            raise RuntimeError("ArtStation portfolio fallback requires an artist profile URL or handle")
        portfolio_url = f"https://{quote(slug, safe='-_.')}.artstation.com/projects"
        projects = await asyncio.to_thread(self._artstation_load_portfolio, slug, portfolio_url)
        selected_projects = projects[offset:offset + limit]
        if not selected_projects:
            return []

        workers = min(get_parallel_workers(), len(selected_projects))
        completed = 0
        discovery_started = time.monotonic()

        def report(message: str, *, transient: bool = False) -> None:
            callback = getattr(self, "search_progress", None)
            if callback:
                elapsed = max(time.monotonic() - discovery_started, 0.0)
                eta = (
                    elapsed / completed * (len(selected_projects) - completed)
                    if completed else None
                )
                callback({
                    "phase": "discover",
                    "total": len(selected_projects),
                    "completed": completed,
                    "workers": workers,
                    "elapsed_seconds": elapsed,
                    "eta_seconds": eta,
                    "message": message,
                    "_transient": transient,
                })

        report(
            f"ArtStation found {len(selected_projects)} projects; matching covers to original assets "
            f"with {workers} worker{'s' if workers != 1 else ''}"
        )
        semaphore = asyncio.Semaphore(workers)

        async def resolve(index: int, project: dict[str, str]):
            nonlocal completed
            async with semaphore:
                title = project.get("title") or project["hash_id"]
                report(
                    f"ArtStation inspecting project {index + 1}/{len(selected_projects)}: {title}",
                    transient=True,
                )
                message = await asyncio.to_thread(
                    self._artstation_resolve_project,
                    slug,
                    portfolio_url,
                    project,
                )
                completed += 1
                selection = message[1].get("artstation_selection") or {}
                score = selection.get("visual_match_score")
                score_text = f" - visual score {score:.1f}" if isinstance(score, (int, float)) else ""
                report(
                    f"ArtStation matched {completed}/{len(selected_projects)}: {title}{score_text}"
                )
                return index, message

        resolved = await asyncio.gather(*(
            resolve(index, project) for index, project in enumerate(selected_projects)
        ))
        return [message for _, message in sorted(resolved, key=lambda item: item[0])]

    @classmethod
    def _artstation_load_portfolio(
        cls,
        slug: str,
        portfolio_url: str,
    ) -> list[dict[str, str]]:
        session = curl_requests.Session(impersonate="chrome")
        try:
            response = cls._artstation_session_get(session, portfolio_url)
            projects = _parse_artstation_portfolio(response.text)
            if not projects:
                raise RuntimeError(f"ArtStation portfolio page for {slug} contained no projects")
            return projects
        finally:
            session.close()

    @staticmethod
    def _artstation_session_get(session, url: str, referer: str = ""):
        last_error: Exception | None = None
        attempts = 0
        for attempt in range(8):
            attempts = attempt + 1
            try:
                response = session.get(url, referer=referer or None, timeout=45)
                if response.status_code == 200:
                    return response
                last_error = RuntimeError(f"HTTP {response.status_code}")
                # Missing/forbidden secondary project assets do not improve
                # after eight retries. Give a 403 one quick retry for an edge
                # challenge, then let the matcher ignore that candidate.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    if response.status_code != 403 or attempt >= 1:
                        break
            except Exception as exc:
                last_error = exc
            if attempt < 7:
                time.sleep(min(0.5 * (attempt + 1), 3.0))
        raise RuntimeError(f"ArtStation did not return {url} after {attempts} attempts: {last_error}")

    @classmethod
    def _artstation_decode_image(cls, session, url: str, referer: str):
        import cv2
        import numpy as np

        response = cls._artstation_session_get(session, url, referer)
        image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"ArtStation comparison image could not be decoded: {url}")
        return image

    @classmethod
    def _artstation_match_image_asset(
        cls,
        session,
        cover_url: str,
        candidates: list[str],
        referer: str,
    ) -> tuple[str, float | None, int, int]:
        if len(candidates) == 1:
            return candidates[0], None, 0, 0

        import numpy as np
        from app.services.media import _visual_match_score, _visual_match_signature

        reference = cls._artstation_decode_image(session, cover_url, referer)
        target_aspect = reference.shape[1] / reference.shape[0]
        signature = _visual_match_signature(reference, target_aspect)
        scored: list[tuple[float, float, str, int, int]] = []
        comparison_errors: list[str] = []
        for candidate_url in candidates:
            preview_url = _artstation_cover_tier(candidate_url, "medium")
            try:
                candidate = cls._artstation_decode_image(session, preview_url, referer)
            except RuntimeError as exc:
                # Process GIFs and stale secondary assets can be forbidden even
                # while the finished image remains public. Ignore only that
                # candidate; the surviving winner must still pass the strict
                # visual threshold below, so this cannot turn into guessing.
                comparison_errors.append(str(exc))
                continue
            height, width = candidate.shape[:2]
            best_visual = float("inf")
            for scale in (1.0, 0.85, 0.7, 0.55, 0.4):
                crop_width = min(width, round(height * target_aspect))
                crop_height = min(height, round(crop_width / target_aspect))
                crop_width = max(1, round(crop_width * scale))
                crop_height = max(1, round(crop_height * scale))
                for y_fraction in np.linspace(0, 1, 5):
                    for x_fraction in np.linspace(0, 1, 5):
                        left = round((width - crop_width) * x_fraction)
                        top = round((height - crop_height) * y_fraction)
                        crop = candidate[top:top + crop_height, left:left + crop_width]
                        best_visual = min(
                            best_visual,
                            _visual_match_score(signature, crop, target_aspect),
                        )
            similarity = _artstation_filename_similarity(cover_url, candidate_url)
            animation_penalty = 1.5 if Path(urlparse(candidate_url).path).suffix.casefold() == ".gif" else 0.0
            combined = best_visual - similarity * 4.0 + animation_penalty
            scored.append((combined, best_visual, candidate_url, width, height))

        if not scored:
            detail = comparison_errors[-1] if comparison_errors else "no decodable candidates"
            raise RuntimeError(f"ArtStation exposed no readable project assets for cover matching: {detail}")
        scored.sort(key=lambda item: item[0])
        _, visual_score, selected, width, height = scored[0]
        if visual_score > 45:
            raise RuntimeError(
                "ArtStation cover could not be matched confidently to a real project asset "
                f"(best score {visual_score:.1f})"
            )
        return selected, visual_score, width, height

    @classmethod
    def _artstation_resolve_project(
        cls,
        slug: str,
        portfolio_url: str,
        project: dict[str, str],
    ) -> tuple[str, dict[str, Any]]:
        session = curl_requests.Session(impersonate="chrome")
        try:
            # Establish the same artist-host session that survives the
            # regional www/API reset before opening the individual project.
            cls._artstation_session_get(session, portfolio_url)
            hash_id = project["hash_id"]
            project_url = f"{portfolio_url}/{hash_id}"
            page = cls._artstation_session_get(session, project_url, portfolio_url)
            image_assets, video_assets = _parse_artstation_project(page.text)
            cover_url = project["cover_url"]
            match_score: float | None = None
            width = height = 0
            if image_assets:
                selected, match_score, width, height = cls._artstation_match_image_asset(
                    session, cover_url, image_assets, project_url
                )
                tiers = _artstation_original_tiers(selected)
                media_url = tiers[0]
                fallbacks = tiers[1:]
                asset_id = _artstation_asset_id(selected)
                method = "portfolio_project_visual_match" if len(image_assets) > 1 else "portfolio_project_single_asset"
            elif video_assets:
                media_url = video_assets[0]
                fallbacks = []
                asset_id = _artstation_asset_id(media_url)
                method = "portfolio_project_video"
            else:
                raise RuntimeError(
                    f"ArtStation project {hash_id} exposed no original image or video assets; "
                    "refusing to import its cover thumbnail"
                )

            raw_timestamp = urlparse(media_url).query.split("&", 1)[0]
            try:
                created_at = datetime.fromtimestamp(int(raw_timestamp), timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                created_at = datetime.now(timezone.utc).isoformat()
            extension = Path(urlparse(media_url).path).suffix.lstrip(".").casefold() or "jpg"
            data = {
                "id": hash_id,
                "hash_id": hash_id,
                "title": project["title"],
                "extension": extension,
                "date": created_at,
                "cover_url": cover_url,
                "width": width,
                "height": height,
                "userinfo": {"username": slug},
                "asset": {"id": asset_id, "image_url": media_url, "width": width, "height": height},
                "_fallback": fallbacks,
                "_http_headers": {"Referer": project_url},
                "artstation_selection": {
                    "method": method,
                    "project_id": hash_id,
                    "candidate_count": len(image_assets) or len(video_assets),
                    "selected_asset_id": asset_id,
                    "visual_match_score": round(match_score, 3) if match_score is not None else None,
                    "detail_api_unavailable": True,
                },
            }
            return media_url, data
        finally:
            session.close()

    @staticmethod
    def _decode_messages(output: str) -> list[tuple[str, dict[str, Any]]]:
        try:
            root = json.loads(output)
        except json.JSONDecodeError:
            root = []
            for line in output.splitlines():
                try:
                    root.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        results: list[tuple[str, dict[str, Any]]] = []

        def walk(value: Any) -> None:
            if isinstance(value, list) and len(value) >= 3 and value[0] == 3 and isinstance(value[1], str) and isinstance(value[2], dict):
                results.append((value[1], value[2]))
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(root)
        return results

    @staticmethod
    def _decode_errors(output: str) -> list[str]:
        try:
            root = json.loads(output)
        except json.JSONDecodeError:
            root = []
            for line in output.splitlines():
                try:
                    root.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        errors: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, list) and len(value) >= 2 and value[0] == -1 and isinstance(value[1], dict):
                errors.append(str(value[1].get("message") or value[1].get("error") or "gallery-dl extraction failed"))
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        walk(root)
        return errors

    def _normalize(self, image_url: str, data: dict[str, Any]) -> RemotePost:
        site = self.site
        raw_number = data.get("num")
        number = (int(raw_number) + 1) if site == "pixiv" and raw_number is not None else int(raw_number or 1)
        base_id = str(data.get("id") or data.get("id_str") or _nested(data, ("project", "id"), default=""))
        if site == "artstation":
            project_hash = str(data.get("hash_id") or _nested(data, ("project", "hash_id"), default=base_id))
            asset_id = str(_nested(data, ("asset", "id"), default=data.get("asset_id") or number))
            remote_id = f"{project_hash}:{asset_id}"
            remote_url = f"https://www.artstation.com/artwork/{project_hash}"
            artist = _nested(data, ("userinfo", "full_name"), ("userinfo", "username"), ("user", "full_name"), default="")
        elif site == "twitter":
            base_id = str(data.get("tweet_id") or data.get("id") or data.get("id_str") or "")
            remote_id = f"{base_id}:{number}"
            author = _nested(data, ("author", "nick"), ("user", "nick"), ("author", "name"), default="i/web")
            screen_name = _nested(data, ("author", "name"), ("user", "name"), ("user", "screen_name"), default="i/web")
            remote_url = f"https://x.com/{screen_name}/status/{base_id}"
            artist = author
        elif site == "pawchive":
            service = str(data.get("service") or "unknown")
            user = str(data.get("user") or "unknown")
            remote_id = f"{service}:{user}:{base_id}:{number}"
            remote_url = f"https://pawchive.pw/{service}/user/{user}/post/{base_id}"
            artist = data.get("username") or user
        else:
            remote_id = f"{base_id}:{number}"
            remote_url = f"https://www.pixiv.net/en/artworks/{base_id}"
            artist = _nested(data, ("user", "name"), ("user", "account"), default="")

        extension = str(data.get("extension") or Path(urlparse(image_url).path).suffix.lstrip(".") or "bin").lower()
        tags = _as_list(data.get("tags") or data.get("hashtags"))
        meta = []
        if extension in {"zip", "gif", "mp4", "webm"}:
            meta.append("animated" if extension in {"zip", "gif"} else "video")
        raw_date = data.get("date") or data.get("create_date") or data.get("published") or datetime.now(timezone.utc).isoformat()
        created_at = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date)
        width = int(data.get("width") or _nested(data, ("asset", "width"), default=0) or 0)
        height = int(data.get("height") or _nested(data, ("asset", "height"), default=0) or 0)
        preview_url = data.get("thumbnail_url") or data.get("preview_url")
        if site == "artstation":
            # The medium portfolio cover is only a matching reference. The
            # selected image_url remains the full-resolution asset (or video).
            preview_url = data.get("cover_url") or _nested(data, ("asset", "image_url"), default=preview_url)
        post = RemotePost(
            provider=site, remote_id=remote_id, remote_url=remote_url, image_url=image_url,
            preview_url=preview_url, width=width, height=height,
            format=extension, md5=data.get("md5") or data.get("hash"),
            tags={key: value for key, value in {
                "artist": [str(artist)] if artist else [], "general": tags, "meta": meta,
            }.items() if value},
            rating=str(data.get("rating") or "") or None,
            score=int(data.get("favorite_count") or data.get("like_count") or data.get("likes") or 0) or None,
            source=data.get("source") or remote_url, parent_id=None, created_at=created_at,
            raw_metadata=_public_metadata(data),
        )
        self._post_cache[(site, remote_id)] = post
        private_headers = data.get("_http_headers") if isinstance(data.get("_http_headers"), dict) else {}
        self._transport_cache[(site, remote_id)] = {
            "fallbacks": [str(value) for value in (data.get("_fallback") or ()) if value],
            "headers": {
                str(key): str(value) for key, value in private_headers.items()
                if str(key).casefold() in {"referer", "user-agent", "accept-encoding"}
            },
        }
        return post

    async def search(self, query: str, cursor: Optional[str] = None, limit: int = 100, sort: str = "latest", date_from: Optional[str] = None, date_to: Optional[str] = None):
        if sort == "oldest":
            raise ValueError(f"{self.site} does not offer reliable oldest-first extraction")
        offset = max(int(cursor or 0), 0)
        limit = min(max(int(limit), 1), provider_max_limit(self.site))
        used_portfolio_fallback = False
        fallback_key = _artstation_slug(query) if self.site == "artstation" else ""
        if fallback_key and self._artstation_portfolio_until.get(fallback_key, 0) > time.monotonic():
            messages = await self._artstation_portfolio_fallback(query, offset, limit)
            used_portfolio_fallback = True
        else:
            try:
                messages = await self._run_gallery_dl(self._target_url(query), offset + 1, offset + limit)
            except RuntimeError as primary_error:
                if self.site != "artstation":
                    raise
                try:
                    messages = await self._artstation_portfolio_fallback(query, offset, limit)
                    used_portfolio_fallback = True
                    if fallback_key:
                        # Avoid repeating gallery-dl's slow retry cycle for
                        # every page while still periodically retesting the
                        # preferred detail API.
                        self._artstation_portfolio_until[fallback_key] = time.monotonic() + 900
                except (RuntimeError, httpx.HTTPError) as fallback_error:
                    raise RuntimeError(
                        f"artstation: detail API failed ({primary_error}); "
                        f"portfolio fallback failed ({fallback_error})"
                    ) from fallback_error
        extracted_count = len(messages)
        last_page_full = extracted_count >= limit
        if self.site == "artstation" and not used_portfolio_fallback:
            # One project can expose many process assets. Keep advancing the
            # raw extractor range until we have the requested number of
            # cover-matched works or actually reach the end of the feed.
            raw_messages = list(messages)
            messages = _select_artstation_cover_messages(raw_messages)
            for _ in range(20):
                if len(messages) >= limit or not last_page_full:
                    break
                request_size = max(1, limit - len(messages))
                more = await self._run_gallery_dl(
                    self._target_url(query), offset + extracted_count + 1,
                    offset + extracted_count + request_size,
                )
                extracted_count += len(more)
                last_page_full = len(more) >= request_size
                raw_messages.extend(more)
                messages = _select_artstation_cover_messages(raw_messages)
            if len(messages) < limit and last_page_full:
                raise RuntimeError("ArtStation cover matching crossed 20 full raw pages without filling the requested count")
            messages = messages[:limit]
        posts = [self._normalize(url, data) for url, data in messages]
        if date_from or date_to:
            filtered = []
            for post in posts:
                try:
                    day = datetime.fromisoformat(post.created_at.replace("Z", "+00:00")).date().isoformat()
                except (ValueError, TypeError):
                    filtered.append(post)
                    continue
                if (not date_from or day >= date_from) and (not date_to or day <= date_to):
                    filtered.append(post)
            posts = filtered
        # A short extracted page is the end of this feed. Returning the
        # requested end offset here incorrectly made the next incremental
        # sync reserve most of its capacity for an empty older page (notably
        # Pawchive creators with fewer assets than the requested amount).
        # Count raw extraction results: ArtStation may discard process assets
        # after extraction, and date filters may discard other results.
        next_cursor = str(offset + extracted_count) if last_page_full else None
        return posts, next_cursor

    async def get_post(self, remote_id: str) -> RemotePost:
        cached = self._post_cache.get((self.site, remote_id))
        if cached:
            return cached
        parts = remote_id.split(":")
        if self.site == "pixiv" and parts:
            target = f"https://www.pixiv.net/en/artworks/{parts[0]}"
        elif self.site == "twitter" and parts:
            target = f"https://x.com/i/web/status/{parts[0]}"
        elif self.site == "artstation" and parts:
            target = f"https://www.artstation.com/artwork/{parts[0]}"
        elif self.site == "pawchive" and len(parts) >= 3:
            target = f"https://pawchive.pw/{parts[0]}/user/{parts[1]}/post/{parts[2]}"
        else:
            raise ValueError(f"Invalid {self.site} post ID")
        for url, data in await self._run_gallery_dl(target, 1, 100):
            post = self._normalize(url, data)
            if post.remote_id == remote_id:
                return post
        raise ValueError(f"{self.site} post asset {remote_id} was not found")

    async def download_image(self, post: RemotePost, dest_path: str, progress: Optional[Callable[[dict], None]] = None) -> None:
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        transport = self._transport_cache.get((self.site, post.remote_id), {})
        headers = dict(transport.get("headers") or {})
        if self.site == "pixiv":
            headers["Referer"] = "https://www.pixiv.net/"
        elif self.site == "artstation":
            headers.setdefault("Referer", post.remote_url)
        elif self.site == "pawchive":
            headers.update({"Referer": post.remote_url, "Accept-Encoding": "identity"})
        urls = [post.image_url, *(transport.get("fallbacks") or [])]
        last_error: Exception | None = None
        total = None
        for attempt, url in enumerate(urls, 1):
            started = time.monotonic()
            downloaded = 0
            try:
                async with self.client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    try:
                        total = int(response.headers.get("content-length", "")) or None
                    except ValueError:
                        total = None
                    with open(dest_path, "wb") as destination:
                        async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                            destination.write(chunk)
                            downloaded += len(chunk)
                            if progress:
                                elapsed = max(time.monotonic() - started, 0.001)
                                speed = downloaded / elapsed
                                progress({"stage": "downloading", "remote_id": post.remote_id, "format": post.format,
                                          "bytes_downloaded": downloaded, "bytes_total": total,
                                          "percent": min(100, round(downloaded * 100 / total, 1)) if total else None,
                                          "speed_bps": speed, "eta_seconds": (total - downloaded) / speed if total and speed else None,
                                          "elapsed_seconds": elapsed, "fallback_attempt": attempt if attempt > 1 else None})
                break
            except httpx.HTTPError as exc:
                last_error = exc
                Path(dest_path).unlink(missing_ok=True)
        else:
            assert last_error is not None
            raise last_error
        if progress:
            elapsed = max(time.monotonic() - started, 0.001)
            progress({"stage": "complete", "remote_id": post.remote_id, "format": post.format,
                      "bytes_downloaded": downloaded, "bytes_total": total, "percent": 100,
                      "speed_bps": downloaded / elapsed, "eta_seconds": 0, "elapsed_seconds": elapsed})

    async def close(self) -> None:
        await self.client.aclose()
