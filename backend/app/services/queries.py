"""Human-readable folder queries and provider-facing query translation."""


def humanize_artist_query(query: str) -> str:
    """Keep provider underscore syntax out of artist-folder UI and storage."""
    if query.strip().startswith(("http://", "https://")):
        return query.strip()
    return " ".join(query.replace("_", " ").split())


def provider_query_for_folder(query: str, folder_type: str) -> str:
    """Treat unqualified words in an artist folder as one artist identity.

    Provider adapters later convert the spaced artist value to their canonical
    underscore form. Explicit filters remain separate from the artist name.
    """
    if folder_type == 'character':
        return '_'.join(query.strip().split())
    readable = humanize_artist_query(query) if folder_type == "artist" else " ".join(query.split())
    if folder_type != "artist" or not readable:
        return readable
    tokens = readable.split()
    if any(token.casefold().startswith("artist:") for token in tokens):
        return readable
    artist_words = [token for token in tokens if ":" not in token and not token.startswith("-")]
    filters = [token for token in tokens if ":" in token or token.startswith("-")]
    if not artist_words:
        return readable
    return " ".join([f"artist:{artist_words[0]}", *artist_words[1:], *filters])


COLLECTION_TYPES = {'artist', 'character', 'tag'}

def validate_collection_query(query, kind, provider=None):
    from app.providers.registry import supported_provider
    from app.providers.booru import BOORU_SITES
    if kind not in COLLECTION_TYPES:
        raise ValueError('Choose artist, character, or tag collection type')
    if not isinstance(query, str) or not query.strip() or len(query) > 1000 or any(ord(c) < 32 for c in query):
        raise ValueError('Search query must be 1-1000 characters without control characters')
    if provider is not None:
        if not supported_provider(provider):
            raise ValueError('Unsupported provider')
        if kind != 'artist' and provider not in BOORU_SITES:
            raise ValueError(f'{provider} currently supports artist profiles only; choose a booru for character/tag searches')
    if kind == 'character' and (query.startswith(('http://', 'https://')) or any(':' in t or t.startswith('-') for t in query.split())):
        raise ValueError('Enter one character name/tag; use Tag / query for combinations or filters')
    return query.strip()


def assert_search_idle(conn, folder_id):
    active = conn.execute("SELECT 1 FROM sync_job WHERE status IN ('queued','running','cancelling') AND (collection_id=? OR kind='all') LIMIT 1", (folder_id,)).fetchone()
    importing = conn.execute("SELECT 1 FROM import_batch WHERE collection_id=? AND status IN ('queued','running','cancelling') LIMIT 1", (folder_id,)).fetchone()
    if active or importing:
        raise ValueError('Finish or cancel active scraping/import jobs before changing this collection search')
