"""Read only media variants advertised for the requested artwork page."""
import json
import re
from urllib.parse import urlparse, quote


def media_url(value):
    try:
        p = urlparse(value)
        return p.scheme == "https" and not p.username and not p.password and not p.port and (p.hostname or "").endswith(".wixmp.com")
    except (ValueError, TypeError):
        return False


def page_candidates(html, deviation_id, allow_thumbnail=False):
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*JSON\.parse\(("(?:\\.|[^"\\])*")\)', html)
    if not match:
        return []
    try:
        state = json.loads(json.loads(match[1].replace("\\'", "'")))
        deviations = state["@@entities"]["deviation"]
        deviation = next(d for d in deviations.values() if str(d.get("deviationId")) == str(deviation_id))
        if deviation.get("isVideo") or deviation.get("isMultiMedia") or deviation.get("isDeleted"):
            return []
        media = deviation["media"]
        base = media["baseUri"]
        token = (media.get("token") or [""])[0]
        pretty = media.get("prettyName", "")
    except (ValueError, KeyError, TypeError, StopIteration):
        return []
    results = []
    for variant in media.get("types", []):
        full = variant.get("t") == "fullview"
        if not full and not allow_thumbnail:
            continue
        for item in [variant, *variant.get("ss", [])]:
            path = item.get("c", "")
            # Never remove blur, crop, watermark, or access-token restrictions.
            # Cropped and blurred previews are not suitable fallback artwork.
            if not path or "/crop/" in path or "blur_" in path:
                continue
            url = path if path.startswith("https://") else base + path.replace("<prettyName>", pretty)
            if not media_url(url):
                continue
            if token:
                url += ("&" if "?" in url else "?") + "token=" + quote(token, safe="")
            try:
                area = int(item.get("w", 0)) * int(item.get("h", 0))
            except (TypeError, ValueError):
                continue
            results.append((not full, -area, url, "fullview" if full else "thumbnail"))
    seen = set()
    return [(url, kind) for _, _, url, kind in sorted(results) if not (url in seen or seen.add(url))]
