> Historical design document. Artist-only terminology, fixed processing policy and any training plans below are superseded by the current [user guide](../USER_GUIDE.md) and [implementation guide](../../IMPLEMENTATION_GUIDE.md). The app does not train models.

# Artist Folder & Training Dataset Builder - Technical Specification

**Version:** 1.1  
**Date:** 2026-09-12  
**Target:** Full-featured v1, flexible timeline, quality over speed

---

## Executive Summary

This application is an **artist-folder training dataset builder** for image model finetuning. It scrapes art from booru sites (Danbooru, Gelbooru, e621, yande.re) and gallery-dl-backed Pixiv, ArtStation, Twitter/X, and Pawchive sources. One artist equals one folder, and every folder owns independent image + ground-truth sidecar pairs with full source provenance. It does not generate natural-language captions.

> **Authoritative terminology and ownership rule (v1.1):** Folder replaces collection everywhere in the product and public API. Images are one-to-many from folder to image, never many-to-many. The legacy SQLite names `collection`, `collection_source`, and `collection_image`, old `collection_id` database columns, internal Python symbols, and the hidden `/api/collections` alias remain only to migrate existing local libraries safely. Any older section below that describes shared files, cross-folder memberships, or library-global hash identity is superseded by this rule.

**What makes this different from a simple scraper:**

1. **Folders are artist-driven.** Each folder normally represents one artist and owns its query, providers, filters, files, and sidecars.

2. **Metadata is versioned and attributed.** Tags from Danbooru on 2026-09-09 are stored as a separate record from the same post's tags six months later. You can diff them, roll back, or keep both. No silent overwrites.

3. **Deduplication is smart and folder-local.** The same artwork can appear on several providers at different resolutions. Exact and perceptual matching consolidate it within one folder, but another folder importing the same content receives an independent copy and sidecar.

4. **Updates are incremental and stateful.** Nightly sync checks each folder's artist query against each source, walking from the last-seen cursor. New posts get added and metadata snapshots are versioned.

5. **Ground-truth tags are directly curated.** Imported provider tags remain immutable provenance, while users can edit one image or batch add/remove/replace tags. Effective tags are written directly to trainer-ready `.txt` sidecars using spaces, never underscores. Every folder may format its own normalized lowercase name with a template such as `Drawn by {artist}`; provider profile names never determine this trigger. Only Danbooru, Gelbooru, and e621 may contribute automatic non-artist trainer tags. Source categories can be included or excluded globally and overridden per folder; metadata is excluded by default.

**Primary users:** ML practitioners building multi-artist or concept-specific datasets for style/character/concept finetunes on Stable Diffusion, SDXL, Flux, or similar models.

**Tech stack:** Python (FastAPI) backend, React + Tailwind frontend, SQLite database, runs as `localhost:8000` in a browser.

## Core Concepts

### Folder

A **folder** is an isolated artist dataset defined by:
- A **query** (e.g., `artist:quasarcake`, `blue_hair 1girl -comic`, `character:hatsune_miku rating:safe`)
- Human-readable artist query input: spaces form one artist identity and provider underscore syntax stays internal for both Sync and Import/Preview
- An optional **artist tag template** whose `{artist}` is the lowercase folder name (default `Drawn by {artist}` for new folders)
- Editable **ground-truth tags** backed by immutable provider metadata
- **Per-folder filters** (resolution floor, aspect ratio, ratings, blocked tags)
- **Source enablement** (which providers to query: Danbooru, Gelbooru, e621, yande.re, Pixiv)

Folders have a **one-to-many** relationship with images. Every image record has exactly one owning `folder_id`. Importing the same bytes or provider post into two folders creates separate database image/source records and separate physical image/sidecar pairs under both folder slugs.

### Image

An **image** is a folder-owned file identified by `(folder_id, SHA256)`. It has:
- Dimensions, format, file size, storage path
- A perceptual hash (pHash) for near-duplicate detection
- Zero or more **source records** (Danbooru post #123, Pixiv artwork #456, etc.)
- Membership in exactly one folder

### Source Record

A **source record** links an image to its origin on a provider:
- Provider (danbooru, pixiv, etc.) and remote post/artwork ID
- Fetch timestamp
- **Full metadata snapshot** as JSON: all tags with categories, rating, score, source URL, parent/child relationships, upload date, uploader
- Tags are also denormalized into a separate table for fast querying

### Ground-Truth Tags

The trainer-facing `.txt` sidecar is a comma-separated ground-truth tag list, not a natural-language caption. Provider tags are preserved unchanged in source records. When enabled, a folder's artist template replaces `{artist}` with the normalized lowercase folder name, producing one stable trigger regardless of profile display names or multi-creator credits. Danbooru, Gelbooru, and e621 are the only providers whose non-artist tags can automatically contribute to ground truth. Yande.re, Pixiv, Twitter/X, ArtStation, Pawchive, and unapproved future sources are provenance-only. Effective tags always group in this order: artist trigger, character, copyright/series, species, general (including character counts), then optional metadata. Provider order and positional edits remain stable within each group. e621's dedicated species taxonomy populates the species group. Gelbooru's flat post tags are typed through its batched tag-index endpoint before ingest. User changes are stored as positional add/remove overrides, so corrections survive metadata refreshes without erasing provenance. Batch replacements keep the old tag's position. Global and per-folder policies choose whether the canonical artist trigger and trusted booru character, copyright, species, general, and metadata tags contribute to effective ground truth. All effective tags use spaces instead of booru underscores.

## Architecture Overview

### Tech Stack

- **Backend:** Python 3.11+, FastAPI, SQLite, httpx for async HTTP, Pillow for image operations, imagehash for perceptual hashing
- **Frontend:** React 18+, Vite, Tailwind CSS, React Query for server state, react-window or react-virtuoso for virtualized grids
- **Scrapers:** Native API clients for boorus (Danbooru, Gelbooru, e621, yande.re), gallery-dl wrapper for Pixiv
- **Storage:** SQLite database at `library/index.db`, images stored in `library/images/`, thumbnails in `library/thumbnails/`

### Component Layout

```
backend/
  app/
    main.py              # FastAPI app entry point
    db.py                # SQLite connection, schema, migrations
    models.py            # Pydantic models for API
    providers/
      base.py            # Provider abstract interface
      booru.py           # Generic booru adapter + per-site configs
      pixiv.py           # gallery-dl wrapper for Pixiv
    services/
      collections.py     # Collection CRUD, query execution
      images.py          # Image ingest, dedup, thumbnail generation
      sync.py            # Incremental update logic
      captions.py        # Template rendering, export
      tags.py            # Tag search, frequency analysis
    routes/
      collections.py
      images.py
      tags.py
      sync.py
      health.py

frontend/
  src/
    components/
      CollectionList.jsx
      ImageGrid.jsx
      ImageDetail.jsx
      TagExplorer.jsx
      CaptionTemplateEditor.jsx
    api/
      client.js          # API wrapper
    routes/
      Dashboard.jsx
      CollectionView.jsx
      TagExplorerView.jsx
      SettingsView.jsx
```

### Data Flow

1. **Collection creation:** User defines query + template → stored in DB
2. **Sync/import:** Provider queries remote API → returns metadata + image URLs → dedup check → download if new → ingest metadata → link to collection(s)
3. **Caption generation:** On export, template + source tags → rendered caption per collection
4. **Incremental updates:** Nightly job walks collections → queries each provider from last cursor → ingests new posts

## Data Model

### SQLite Schema

```sql
-- Legacy table name: artist folders defined by queries
CREATE TABLE collection (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  slug TEXT UNIQUE NOT NULL,  -- filesystem-safe identifier
  type TEXT NOT NULL,  -- 'artist', 'character', 'concept', 'custom'
  query TEXT NOT NULL,  -- tag query string
  caption_template TEXT NOT NULL,  -- JSON config
  filters TEXT NOT NULL,  -- JSON: resolution, aspect, rating, format, tag blacklist
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_sync_at TEXT,
  enabled INTEGER DEFAULT 1
);

-- Which sources are enabled per folder (legacy column name retained)
CREATE TABLE collection_source (
  id INTEGER PRIMARY KEY,
  collection_id INTEGER NOT NULL,
  provider TEXT NOT NULL,  -- 'danbooru', 'gelbooru', 'e621', 'yandere', 'pixiv'
  last_cursor TEXT,  -- provider-specific: post ID for boorus, artwork ID for pixiv
  enabled INTEGER DEFAULT 1,
  FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
  UNIQUE (collection_id, provider)
);

-- Images: independently owned by one folder
CREATE TABLE image (
  id INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL,
  md5 TEXT,  -- for booru cross-reference
  phash INTEGER,  -- perceptual hash as uint64
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  format TEXT NOT NULL,  -- 'jpg', 'png', 'webp'
  file_size INTEGER NOT NULL,
  path TEXT NOT NULL,  -- relative to library/images/
  thumb_path TEXT,  -- relative to library/thumbnails/
  added_at TEXT NOT NULL,
  folder_id INTEGER NOT NULL,
  UNIQUE (folder_id, sha256),
  FOREIGN KEY (folder_id) REFERENCES collection(id) ON DELETE CASCADE
);
CREATE INDEX idx_image_md5 ON image(md5);
CREATE INDEX idx_image_phash ON image(phash);

-- Legacy compatibility/review row. Its collection_id must equal image.folder_id.
CREATE TABLE collection_image (
  collection_id INTEGER NOT NULL,
  image_id INTEGER NOT NULL,
  added_at TEXT NOT NULL,
  PRIMARY KEY (collection_id, image_id),
  FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
  FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
);
CREATE INDEX idx_collection_image_collection ON collection_image(collection_id);
CREATE INDEX idx_collection_image_image ON collection_image(image_id);

-- Source records: where images came from
CREATE TABLE image_source (
  id INTEGER PRIMARY KEY,
  image_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  remote_id TEXT NOT NULL,  -- post ID or artwork ID
  remote_url TEXT,
  fetched_at TEXT NOT NULL,
  metadata TEXT NOT NULL,  -- full JSON from provider
  version INTEGER DEFAULT 1,  -- for tracking metadata updates
  UNIQUE (image_id, provider, remote_id, version),
  FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
);
CREATE INDEX idx_source_provider_remote ON image_source(provider, remote_id);
CREATE INDEX idx_source_image ON image_source(image_id);

-- Tags: denormalized from source metadata for fast queries
CREATE TABLE image_tag (
  image_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL,
  category TEXT NOT NULL,  -- 'artist', 'character', 'copyright', 'species', 'general', 'meta'
  tag TEXT NOT NULL,
  score INTEGER,  -- tag vote count/confidence if available
  FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE,
  FOREIGN KEY (source_id) REFERENCES image_source(id) ON DELETE CASCADE
);
CREATE INDEX idx_tag_image ON image_tag(image_id);
CREATE INDEX idx_tag_category_tag ON image_tag(category, tag);

-- Rejected posts: never download these again
CREATE TABLE rejected_post (
  provider TEXT NOT NULL,
  remote_id TEXT NOT NULL,
  rejected_at TEXT NOT NULL,
  reason TEXT,  -- optional note
  PRIMARY KEY (provider, remote_id)
);

-- Optional export records per collection; the canonical `.txt` sidecar is the curated ground-truth tag list
CREATE TABLE collection_caption (
  collection_id INTEGER NOT NULL,
  image_id INTEGER NOT NULL,
  caption TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  template_version INTEGER NOT NULL,  -- increment when template changes
  PRIMARY KEY (collection_id, image_id),
  FOREIGN KEY (collection_id) REFERENCES collection(id) ON DELETE CASCADE,
  FOREIGN KEY (image_id) REFERENCES image(id) ON DELETE CASCADE
);
```

## Provider System

### Provider Interface

All scrapers implement a common `Provider` abstract base class:

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from dataclasses import dataclass

@dataclass
class RemotePost:
    provider: str
    remote_id: str
    remote_url: str
    image_url: str
    width: int
    height: int
    format: str
    md5: Optional[str]
    tags: dict[str, list[str]]  # category -> list of tags
    rating: Optional[str]
    score: Optional[int]
    source: Optional[str]
    parent_id: Optional[str]
    created_at: str
    raw_metadata: dict

class Provider(ABC):
    @abstractmethod
    async def search(
        self, 
        query: str, 
        cursor: Optional[str] = None, 
        limit: int = 100
    ) -> tuple[list[RemotePost], Optional[str]]:
        """Search posts matching query. Returns (posts, next_cursor)."""
        pass
    
    @abstractmethod
    async def download_image(self, post: RemotePost, dest_path: str) -> None:
        """Download the image file to dest_path."""
        pass
    
    @abstractmethod
    async def get_post(self, remote_id: str) -> RemotePost:
        """Fetch metadata for a single post by ID."""
        pass
```

### Booru Provider (Danbooru, Gelbooru, e621, yande.re)

**Implementation:** One generic `BooruProvider` class parameterized by a site config.

**Site configs** (JSON or Python dicts):

```python
BOORU_SITES = {
    'danbooru': {
        'base_url': 'https://danbooru.donmai.us',
        'api_path': '/posts.json',
        'page_param': 'page',  # supports keyset: page=a{id} / page=b{id}
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
        'rate_limit': 1.0,  # seconds between requests
    },
    'gelbooru': {
        'base_url': 'https://gelbooru.com',
        'api_path': '/index.php',
        'page_param': 'pid',  # offset-based
        'tag_param': 'tags',
        'extra_params': {'page': 'dapi', 's': 'post', 'q': 'index', 'json': '1'},
        'max_limit': 100,
        'tag_categories': None,  # needs separate tag API call
        'image_url_field': 'file_url',
        'rate_limit': 1.0,
    },
    'e621': {
        'base_url': 'https://e621.net',
        'api_path': '/posts.json',
        'page_param': 'page',  # keyset like danbooru
        'tag_param': 'tags',
        'limit_param': 'limit',
        'max_limit': 320,
        'requires_user_agent': True,  # must include app name + contact
        'tag_categories': {  # nested in tags object
            'artist': 'artist',
            'character': 'character',
            'copyright': 'copyright',
            'general': 'general',
            'meta': 'meta',
        },
        'rate_limit': 1.0,
    },
    'yandere': {
        'base_url': 'https://yande.re',
        'api_path': '/post.json',
        'page_param': 'page',
        'tag_param': 'tags',
        'limit_param': 'limit',
        'max_limit': 100,
        'rate_limit': 1.0,
    }
}
```

**Key behaviors:**
- Danbooru and e621 support **keyset pagination** (`page=a{id}` for after-id, `page=b{id}` for before-id), which avoids deep pagination limits. Use this for incremental sync.
- Gelbooru uses offset pagination; don't paginate too deep.
- e621 requires a descriptive User-Agent or it returns 403.
- Rate limiting: 1 req/sec per site by default. Use `asyncio.Semaphore` + `asyncio.sleep`.

### Pixiv Provider (via gallery-dl)

**Implementation:** Shell out to `gallery-dl` with appropriate flags.

**Search/list:**
```bash
gallery-dl --dump-json --range 1-100 "https://www.pixiv.net/users/{user_id}/artworks"
```

Returns one JSON object per line (newline-delimited JSON). Parse each line into a `RemotePost`.

**Download:**
```bash
gallery-dl --dest library/images/{collection_slug} "https://www.pixiv.net/artworks/{artwork_id}"
```

**Auth:** Assumes `~/.config/gallery-dl/config.json` has a valid pixiv refresh token. On first run, if pixiv is enabled and not configured, guide the user through `gallery-dl oauth:pixiv`.

**Ugoira handling:** Pixiv ugoira (animated) should be detected and either skipped or converted to video/gif depending on filter settings.

## Deduplication & Identity

### Four-Tier Strategy

**Tier 1: Exact hash match (SHA256 or MD5)**
- If an image with the same SHA256 already exists, reuse it.
- Boorus share MD5 hashes, so check MD5 against existing images and remote posts.
- Catches: identical files uploaded to multiple boorus.

**Tier 2: Source linkage**
- Danbooru and other boorus store `pixiv_id` and `source` fields pointing to the original upload.
- When ingesting a booru post, extract the pixiv ID from metadata and check if we already have that pixiv artwork.
- Catches: same artwork on Danbooru and Pixiv at different resolutions.

**Tier 3: Perceptual hash (pHash) with Hamming distance**
- Compute pHash (8x8 DCT-based, 64-bit uint) on ingest using `imagehash.phash()`.
- On new image, query existing images where `hamming_distance(phash, new_phash) <= threshold`.
- Threshold: 5–10 for tight matching (resized/recompressed variants), 15–20 for loose (cropped/edited).
- Catches: resized, recompressed, lightly edited versions.

**Tier 4: CLIP embeddings (optional, future)**
- Generate 512D CLIP embedding per image.
- Cluster similar images for map view or semantic dedup.
- Out of scope for v1, but the architecture should allow it later.

### When duplicates are found

**If exact match (Tier 1 or 2):**
- Reuse the existing image record only when it is already in the requesting folder.
- Add a new `image_source` record linking the new provider + remote_id to that image.
- If the match exists only in another folder, create a separate image/source/sidecar pair.

**If near-duplicate (Tier 3):**
- Present both to the user as a **duplicate group** in the import flow.
- Let the user choose: keep both, keep highest resolution, keep both but mark one primary.
- Winner selection heuristic:
  1. Highest pixel count (width * height)
  2. Original format over recompressed (PNG > JPEG, original > sample)
  3. Source priority (Pixiv original > Danbooru > Gelbooru repost)

### Folder Ownership

An image belongs to exactly one artist folder. If identical bytes are imported for two artists:

- Image `img_001.jpg` (SHA256: `abc123...`)
- Tagged: `artist:quasarcake blue_hair 1girl long_hair`
- Folder A owns its image row, file, source records, overrides, and sidecar.
- Folder B owns a distinct image row, file, source records, overrides, and sidecar.

The files are independently stored at `library/images/<folder-a>/<sha256>.<ext>` and `library/images/<folder-b>/<sha256>.<ext>`. Editing or deleting one folder cannot change the other pair.

## UI Structure

### Layout

**Left Sidebar (240px fixed width):**
- App logo/name at top
- "New Collection" button
- Search box (filters collections by name/query)
- Collections list (scrollable):
  - Each item shows: name, image count, status dot (syncing/error/idle)
  - Click to view that collection
  - Right-click context menu: Edit, Sync Now, Delete
- "All Images" meta-collection at top (shows everything, deduplicated)
- Status footer: "Last sync: 2h ago • 12,847 images"

**Top Navigation Bar:**
- Current view breadcrumb (e.g., "Collections / quasarcake / Gallery")
- Tab switcher (context-dependent):
  - In a collection: Gallery | Tags | Sources | Settings
  - At root: Dashboard | Tag Explorer | Settings
- Global actions: Sync All, Export, Settings icon

**Main Content Area:**
- Renders the active view (see below)

**Image Detail Overlay (modal):**
- Opens when clicking an image in any grid
- Left 60%: Image preview (zoomable, pan)
- Right 40%: Metadata panel
  - Filename, dimensions, format, file size
  - Collection membership badges (which collections include this)
  - Source records (expandable per provider):
    - Provider icon + post ID + fetch date
    - Tags by category (artist/character/copyright/general/meta)
    - Rating, score, source URL
    - "View original" link
  - Actions: Edit tags, Remove from collection, Delete, Reject
- Navigation: Left/right arrows or J/K keys to move through images

### Views

**Dashboard (root view):**
- Summary cards: Total images, Total collections, Storage used, Last sync
- Recent additions grid (last 50 images added, any collection)
- Sync status table: collection name, last sync, status, images added in last run
- Quick actions: Import artist list, Create collection, Run sync

**Collection Gallery View:**
- Virtualized masonry or justified grid (react-window or react-virtuoso)
- Thumbnail size slider (small/medium/large)
- Sort controls: Date added, Date created (remote), Score, Random
- Filter bar: search by tags, rating, source
- Selection mode: checkbox overlays for batch actions (export, delete, move to collection)
- Bottom toolbar when items selected: "Export N images", "Remove from collection", "Add to..."

**Collection Tags View:**
- Two-column layout:
  - Left: Tag search box + category filter buttons (All/Artist/Character/General/Meta)
  - Right: Tag list (virtualized, sorted by frequency descending)
    - Each row: tag name, count, category badge
    - Click tag → filters gallery to images with that tag
    - Right-click: Add to query, Exclude from query, Block from export

**Collection Sources View:**
- Per-source tabs (Danbooru, Gelbooru, e621, yande.re, Pixiv)
- For each:
  - Enable/disable toggle
  - Last cursor value (read-only display)
  - "Sync now" button
  - Status: idle / syncing / error (with message)
  - Last sync timestamp + images added

**Collection Settings View:**
- Query editor (text input + syntax help)
- Ground-truth tag editor and preview-fallback settings
- Filters:
  - Min resolution (width/height sliders)
  - Aspect ratio range (sliders)
  - Allowed ratings (checkboxes: safe/questionable/explicit)
  - Blocked tags (tag input)
  - Format filters (checkboxes: extract a still from original media, allow static-preview fallback)
- Export settings:
  - Export directory (file picker)
  - Symlink vs copy
  - Numbered folder prefix (e.g., `10_quasarcake`)

**Tag Explorer View:**
- Top bar:
  - Search box (tag query input)
  - Source selector (which provider to search)
  - "Search" button
- Results grid (virtualized):
  - Thumbnails loaded from provider (not downloaded yet)
  - Overlay: post ID, dimensions, score
  - Hover: quick metadata preview
  - Click: opens detail modal with "Add to collection" / "Create collection" actions
- Batch actions: Select multiple → "Add to collection" or "Create new collection from selection"

## Ground-Truth Tag Curation

- The effective tag list starts from normalized provider tags.
- Single-image editing replaces the effective list without modifying source records.
- Batch actions add or remove one or more tags, or replace one tag across selected images.
- Batch replacement retains the old tag's exact effective-list position so artist and character triggers never move to the end of trainer sidecars.
- Overrides and undo history are stored in SQLite.
- Every edit immediately rewrites the canonical UTF-8 `.txt` sidecar.
- Underscores are normalized to spaces at the API boundary and never appear in effective tags.

## Sync & Update System

### Incremental Update Strategy

Each `collection_source` record tracks a `last_cursor` — the most recent post/artwork ID seen from that provider for that collection.

**Sync algorithm per collection per source:**

1. Query the provider with the collection's query, sorted by ID descending (newest first)
2. If `last_cursor` exists, use keyset pagination starting from that cursor (e.g., `page=a{last_cursor}` for Danbooru)
3. Fetch posts in batches of 100–200 until:
   - We hit a post ID <= `last_cursor` (caught up), OR
   - No more results returned, OR
   - Rate limit / error threshold reached
4. For each new post:
   - Check if rejected (skip)
   - Check dedup tiers (exact hash, source linkage, pHash)
   - If new or near-duplicate: download image, ingest metadata, link to collection
   - If exact match: just link to collection (image already exists)
5. Update `last_cursor` to the highest post ID seen
6. Update `collection.last_sync_at` timestamp

**For Pixiv (via gallery-dl):**
- Cursor is the last artwork ID
- Query: `gallery-dl --dump-json --range 1-500 {artist_url}`
- Parse JSON lines, filter to IDs > last_cursor

**Nightly job:**
- Runs automatically (cron-like scheduler or manual trigger)
- Iterates all enabled collections
- For each enabled source in each collection, run sync
- Aggregate results: total new images, failed collections, duration
- Store sync report in DB + optionally email/notify user

**Rate limiting:**
- Global semaphore per provider (e.g., max 2 concurrent requests to Danbooru)
- Per-provider sleep between requests (1–2 seconds)
- Exponential backoff on 429/503 errors
- Fail gracefully: log error, continue to next collection

### Metadata Re-sync

Separately from new post detection, periodically re-fetch metadata for existing posts to catch tag updates:

- Once per week, sample 10% of images per collection
- Fetch fresh metadata from provider by remote_id
- If tags differ, create a new `image_source` record with incremented `version`
- Keep old version for diffing

This is lower priority than initial sync; implement in phase 2.

## Import Flows

### 1. Bulk Artist List Import

**Trigger:** User clicks "Import Artist List" from Dashboard or menu.

**Flow:**
1. File picker or textarea: paste artist names, one per line
2. Parse into list of names
3. For each name, attempt to resolve against providers:
   - Query Danbooru `/artists.json?search[name]={name}` → get artist tag
   - Query Pixiv (if configured) by searching artist name → get user ID (via gallery-dl or search)
   - Query other boorus similarly
4. Show resolution table:
   - Columns: Input name, Danbooru match, Pixiv match, Gelbooru match, Status
   - Status: ✓ Resolved / ⚠ Ambiguous (multiple matches) / ✗ Not found
5. User reviews, selects correct matches for ambiguous rows, skips unresolved
6. For each resolved artist:
   - Create collection with type='artist', query=`artist:{danbooru_tag}`
   - Set default ground-truth tag policy (preserve provider tags; no natural-language captions)
   - Enable all sources by default
   - Set filters to default (minimum 512px on both axes, skip comics, skip AI)
7. Show summary: "Created 47 collections, skipped 3"
8. Offer "Start full sync now" button → queues backfill for all new collections

### 2. Single Collection Creation (Manual)

**Trigger:** User clicks "New Collection" button.

**Flow:**
1. Modal/form with fields:
   - Name (required)
   - Type (dropdown: Artist / Character / Concept / Custom)
   - Query (text input with syntax help)
   - Sources (checkboxes: Danbooru, Gelbooru, e621, yande.re, Pixiv)
2. Auto-suggest:
   - If type=Artist, suggest query format `artist:{name}`
   - If type=Character, suggest `character:{name}`
3. User fills form, clicks Create
4. Collection created with default template and filters
5. Immediately open Collection Settings view for refinement
6. Offer "Start sync now" button

### 3. Tag Explorer → Collection

**Trigger:** User searches a tag in Tag Explorer, selects images.

**Flow:**
1. User enters query in Tag Explorer (e.g., `blue_hair 1girl -comic`)
2. Results grid shows thumbnails from selected source (not downloaded)
3. User selects 10–50 images via checkboxes
4. Clicks "Add to collection" → dropdown/modal:
   - Option A: Select existing collection
   - Option B: "Create new collection from these"
5. If creating new:
   - Pre-fill name and query from search
   - Set type=Concept by default
   - Confirm ground-truth tag policy (provider tags plus user overrides)
6. Download selected images, ingest metadata, link to target collection
7. Show toast: "Added 37 images to {collection_name}"

### 4. Single Image/Post Addition

**Trigger:** User pastes a post URL (e.g., `https://danbooru.donmai.us/posts/123456`).

**Flow:**
1. Parse URL → extract provider + remote_id
2. Fetch metadata from provider
3. Show preview modal:
   - Image preview
   - Tags, rating, dimensions
   - "Add to collections" multi-select
4. User selects target collection(s), clicks Add
5. Download, ingest, link to selected collections

## Export System

### Dataset Export

**Trigger:** User validates an artist folder in its Dataset tab, then creates an export when it is ready.

**Output structure:**

```
library/exports/
  folder_slug/
    export_id/
      sha256.jpg
      sha256.txt
      sha256.webp
      sha256.txt
      manifest.json
```

**Per folder:**
- Create a new immutable export-ID directory; never overwrite an earlier export.
- For every canonical image, copy or hardlink the image and its existing curated UTF-8 sidecar.
- If hardlinking is unavailable for a pair, copy it and record the fallback warning.
- Use the SHA-256 basename to avoid export filename collisions.
- Write `manifest.json` with file hashes/dimensions, review state, effective tags, derived-media origin, and every versioned source metadata snapshot.
- Do not generate, rewrite, or template captions during export.

Natural-language caption generation and caption-only regeneration are out of scope. Ground-truth tag sidecars are edited in the curation workflow before validation.

### Trainer Image Normalization

- Apply to every newly imported still immediately before hashing and storage.
- Decode and apply EXIF orientation first.
- If the longest side exceeds 2000px, resize proportionally with OpenCV `INTER_AREA`; never upscale.
- Encode exactly once after resizing: JPEG input remains JPEG at quality 100 with 4:4:4 chroma, while every non-JPEG input becomes lossless WebP using method 4.
- Animation, video, and archive frames use the same final normalization.
- Download each motion original once, sample up to three distinct, separated full-resolution frames, and store each as a separate image with a stable frame-specific source ID. Include the cover-matched frame for ArtStation-hosted videos. A short/static clip or a frame failing quality filters may yield fewer than three images; never invent duplicate images.
- Preserve the source/output formats, dimensions, algorithm, and encoder settings in source provenance.
- Do not silently rewrite existing library files. Phase 4 validation identifies legacy files that require explicit re-import.

### Validation

Before export:
- Check for missing canonical files and sidecars (error).
- Decode every canonical still and verify its indexed SHA-256 (error).
- Check unsupported still formats and legacy provider-preview imports; both are export-blocking errors under the original-quality policy.
- Reject trainer files outside JPEG/lossless-WebP, below the folder's minimum decoded dimensions/aspect policy, or with a longest side above 2000px.
- Detect case-insensitive canonical and sidecar path collisions (error; never auto-rename).
- Compare sidecars with effective curated tags and reject stale or empty ground truth.
- Show the read-only validation report and block export until all errors are resolved.

## Filtering System

### Per-Collection Filters

Stored as JSON in `collection.filters` field:

```json
{
  "min_width": 512,
  "min_height": 512,
  "max_aspect_ratio": 3.0,
  "min_aspect_ratio": 0.33,
  "allowed_ratings": ["safe", "questionable"],
  "blocked_tags": ["comic", "manga", "multiple_pages", "ai-generated", "ai-assisted"],
  "allowed_formats": ["jpg", "png", "webp"],
  "blocked_formats": ["gif", "webm", "mp4"],
  "max_file_size_mb": 50,
  "skip_ugoira": true
}
```

**Application points:**

1. **At query time:** Some filters can be pushed to the provider query:
   - Danbooru still-image searches may append `width:>=512 height:>=512 -comic -ai-generated`; archive/video dimensions must be checked after extraction
   - Other boorus: similar tag-based filters in query string

2. **At fetch time:** After receiving post metadata, before downloading:
   - Check dimensions, aspect ratio, rating, tags, format
   - If fails filter: skip download, optionally log as "filtered"

3. **Post-download (fallback):** If provider doesn't expose all metadata pre-download:
   - Download to temp location
   - Inspect with Pillow
   - If fails filter: delete temp file, mark as filtered

### Global Default Filters

Stored in app settings, applied to new collections:

```json
{
  "min_width": 512,
  "min_height": 512,
  "max_aspect_ratio": 3.0,
  "min_aspect_ratio": 0.33,
  "allowed_ratings": ["safe", "questionable", "explicit"],
  "blocked_tags": ["ai-generated", "ai-assisted"],
  "allowed_formats": ["jpg", "png", "webp"],
  "blocked_formats": ["gif", "webm", "mp4"],
  "skip_ugoira": true
}
```

User can edit in Settings view, and they become the template for new collections.

### UI for Filter Editing

In Collection Settings, a form with:
- Resolution sliders (min width/height)
- Aspect ratio range slider (with presets: square, portrait, landscape, wide)
- Rating checkboxes (Safe, Questionable, Explicit)
- Tag blocklist: tag input with autocomplete
- Format toggles: "Skip GIF", "Skip WebM", "Skip Ugoira"
- Max file size input

Changes apply to future syncs, not retroactively to existing images (unless user runs "Re-apply filters" action).

## Tag Explorer

### Purpose

A standalone view for discovering content before committing to scrape it. Lets users:
- Search by tag on any provider
- Preview thumbnails and metadata without downloading
- Create collections from search results
- Add selected posts to existing collections

### UI Layout

**Top bar:**
- Search input (tag query syntax)
- Provider dropdown (Danbooru, Gelbooru, e621, yande.re, Pixiv)
- Search button
- Filter toggles: Safe, Questionable, Explicit

**Results grid:**
- Virtualized grid of thumbnails (loaded from provider, not local)
- Each thumbnail shows:
  - Image (lazy-loaded from provider's thumbnail URL)
  - Post ID badge
  - Dimensions overlay
  - Score/favorites count
  - Checkbox for selection
- Hover: Show quick metadata tooltip (rating, top 5 tags)
- Click: Open detail modal

**Detail modal (from Tag Explorer):**
- Similar to main image detail view, but:
  - Image not downloaded yet (show "Not in library" badge)
  - Actions: "Download to collection", "Reject post", "Open on provider"
  - If already in library: show which collections, link to local view

**Bottom toolbar (when items selected):**
- "Add to collection" dropdown (lists existing collections)
- "Create new collection from selection" button
- Selection count: "14 selected"

### Backend API

**Endpoint:** `GET /api/explore?provider={provider}&query={query}&cursor={cursor}&limit={limit}`

**Behavior:**
- Calls provider's search method
- Returns post metadata WITHOUT downloading images
- Returns thumbnail URLs from provider
- Pagination: returns next_cursor for infinite scroll

**Download from explorer:**
- `POST /api/explore/download` with body: `{provider, remote_ids[], collection_id}`
- Queues download jobs for selected posts
- Returns job IDs for progress tracking

### Search Syntax

Support booru tag syntax:
- `artist:quasarcake` (namespace search)
- `blue_hair 1girl` (AND)
- `~blue_hair ~green_hair` (OR)
- `-comic` (NOT)
- `width:>=1024 height:>=1024` (metatags)
- `rating:safe` (rating filter)

Display syntax help tooltip next to search box.

## Dataset Health & QA

### Health Dashboard

A dedicated view (accessible from Dashboard or per-collection) showing dataset quality metrics and issues:

**Global metrics:**
- Total images, total size
- Format distribution (pie chart: JPG, PNG, WebP, other)
- Resolution distribution (histogram)
- Aspect ratio distribution
- Rating distribution
- Images per collection (bar chart)

**Issue detection:**

1. **Low resolution images**
   - List images below the universal threshold (<512px on either axis)
   - Per-folder settings may raise this floor but cannot lower it

2. **Extreme aspect ratios**
   - List images wider than 3:1 or taller than 1:3 (likely banners/comics)
   - Preview + option to remove or keep

3. **Problematic formats**
   - GIF, WebM, MP4 (animated, most trainers can't use)
   - Ugoira (Pixiv animations)
   - Action: Extract one representative still from the original media. ZIP/CBZ and RAR/CBR originals use the naturally ordered middle image frame, preferring still-frame sequences over contained videos. If original extraction fails, skip and report clearly; never use a low-resolution provider thumbnail for training. Then apply the common 2000px JPEG/lossless-WebP training normalization. The derived image retains the original media format, URL, archive member, and decoder in provenance.

4. **Color space issues**
   - CMYK images (should be RGB)
   - Grayscale (might want color-only for style training)
   - Images with alpha channel (some trainers don't handle)
   - Action: Convert or flag

5. **Corrupt files**
   - Images that fail to load with Pillow
   - Truncated or incomplete downloads
   - Action: Re-download or delete

6. **Near-duplicates**
   - List groups of images with pHash distance < 5 (very similar)
   - Show side-by-side, let user pick best or keep both

7. **Missing metadata**
   - Images without source records (orphans)
   - Images without any tags
   - Action: Attempt re-lookup or manual tagging

8. **Oversized files**
   - Files >20MB (might want to resize/compress)
   - Action: Compress or flag

**Batch actions:**
- "Fix all" button per issue type (applies safe auto-fixes)
- "Review" button opens filtered gallery for manual triage
- "Ignore" button to mark issues as false positives

### Per-Collection Health Summary

In each collection's view, show a summary card:
- Image count, total size
- Issues count by type (clickable to filter gallery)
- Balance metric: if this is part of multi-artist dataset, show relative size vs others
- Last QA run timestamp
- "Run health check" button

## Pixiv Authentication Setup

### Challenge

Pixiv has no public API. Access requires:
- A valid user account
- OAuth refresh token or session cookies
- Proper headers (`Referer`, `User-Agent`)

**Solution:** Leverage gallery-dl's existing Pixiv support, which handles auth and keeps it updated.

### Setup Flow

**On first run, if Pixiv is enabled in any collection:**

1. Check if gallery-dl is installed: `gallery-dl --version`
   - If not found, show error: "gallery-dl not installed. Install with: pip install gallery-dl"

2. Check if Pixiv is already configured:
   - Look for `~/.config/gallery-dl/config.json` (Linux/Mac) or `%APPDATA%/gallery-dl/config.json` (Windows)
   - Parse JSON, check if `extractor.pixiv.refresh-token` exists and is non-empty

3. If not configured, show guided setup modal:
   - **Step 1:** "We'll use gallery-dl to authenticate with Pixiv. This is a one-time setup."
   - **Step 2:** Button: "Authenticate with Pixiv" → runs `gallery-dl oauth:pixiv` in a terminal subprocess
   - This opens a browser window for Pixiv login, then gallery-dl saves the refresh token
   - **Step 3:** Once subprocess completes, verify config exists, show success message

4. Test Pixiv access:
   - Run `gallery-dl --dump-json --range 1 "https://www.pixiv.net/users/11"`
   - If succeeds: Pixiv ready
   - If fails: Show error message with troubleshooting link

**Settings page:**
- "Pixiv Authentication" section
- Status: ✓ Configured / ✗ Not configured / ⚠ Expired
- Button: "Re-authenticate" (re-runs oauth flow)
- Button: "Test connection" (tries sample request)
- Link: "gallery-dl Pixiv documentation"

### Refresh Token Expiry

Pixiv refresh tokens expire periodically (weeks to months). When a sync fails with auth error:
- Log the error
- Mark collection_source as status='auth_required'
- Show notification: "Pixiv authentication expired. Please re-authenticate."
- Guide user back to settings to re-run oauth flow

## Build Phases

### Phase 1: Foundation (Weeks 1–4)

**Goal:** Core data model, basic UI shell, single-provider scraping.

**Deliverables:**
- SQLite schema created with migrations
- FastAPI backend skeleton with routes
- React frontend with routing, basic layout (sidebar + main content)
- Danbooru provider implementation (native API client)
- Collections CRUD: create, list, view
- Image ingest pipeline: download → hash → thumbnail → store
- Basic gallery view (static grid, no virtualization yet)
- Image detail modal with metadata display

**Acceptance:** Can create a collection with query `artist:quasarcake`, sync from Danbooru, view results in a grid.

---

### Phase 2: Multi-Provider & Dedup (Weeks 5–7)

**Goal:** Add remaining booru providers, implement deduplication.

**Deliverables:**
- Gelbooru, e621, yande.re providers (using generic booru adapter + configs)
- Perceptual hash computation on ingest (imagehash.phash)
- Dedup tier 1 & 2 (exact hash, MD5 cross-reference)
- Dedup tier 3: 64-bit pHash + dHash and Czkawka-inspired 256-bit gradient hashing with Hamming distance
- XnView-style visual-content scoring using color layout and aspect similarity (inspired behavior, not XnView's proprietary algorithm)
- Strict, balanced, and broad scan profiles with cached fingerprints and folder-local matching
- Duplicate review UI with side-by-side metrics and keep A, keep B, highest quality, keep both, and not-duplicate decisions
- Perceptual candidates are never deleted automatically; confirmed merges preserve memberships, sources, tags, captions, and an audit record
- Folder ownership: one folder to many independently stored image/sidecar pairs
- Per-folder source enabling/disabling

**Acceptance:** Can scrape the same artist from multiple boorus; exact matches are reused within its folder, visual matches are queued for review, and confirmed merges retain all source provenance without ever merging across folders.

---

### Phase 3: Ground-Truth Tag Curation & Original-Media Frame Extraction (Weeks 8–10)

**Goal:** Correct and maintain trainer-facing tag sidecars without natural-language generation.

**Deliverables:**
- Immutable imported source tags plus editable add/remove overrides
- Single-image comma-separated ground-truth tag editor
- Batch add/remove for gallery-selected images
- Collection-wide effective tag counts
- Immediate UTF-8 sidecar synchronization with spaces instead of underscores
- Optional per-folder artist tag template, defaulting to `Drawn by {artist}` and applied to every credited artist
- Durable undo records for single and batch edits
- Original-media frame extraction for animation/video/archive posts: download the original once, extract a representative full-quality frame, apply the common 2000px JPEG/lossless-WebP training normalization, and retain the original URL/format in provenance. Archive posts support ZIP/CBZ and RAR/CBR, record the selected member, and prefer still sequences to contained videos. Skip cleanly when original extraction fails; provider thumbnails are not training data.

**Acceptance:** Can select any set of images, add or remove tags in one action, edit an individual image's full ground-truth list, undo mistakes, and verify that provider metadata remains unchanged while sidecars match the effective tags. Animation/video/archive originals produce a visibly marked frame normalized to the trainer policy when decodable; extraction failures skip without downloading or storing the provider preview and without an error cascade.

---

### Phase 4: Incremental Sync & Updates (Weeks 11–13)

**Goal:** Nightly updates, cursor tracking, stateful sync.

**Deliverables:**
- Cursor tracking per collection_source
- Incremental sync algorithm (keyset pagination for boorus)
- Sync job queue and execution
- Rate limiting (per-provider semaphores, sleep between requests)
- Configurable bounded per-job parallelism (1-4 workers, default 2) for downloads and local processing; provider request pacing still applies
- Exponential backoff on errors
- Sync status UI (last sync time, images added, errors)
- Separate current-file transfer progress with downloaded/total size, average MB/s, ETA when total size is known, and processing/extraction state
- Display each active worker/file separately while retaining one overall completed-post progress bar
- Manual "Sync now" action per collection
- Global "Sync all" with progress reporting
- Sync report/digest (summary of results)
- Durable SQLite job records and bounded logs survive restarts; interrupted, uncommitted pages resume idempotently
- Persistent interval schedule with configurable per-source limit and manual Run Now; each run checks new posts before continuing older backfill
- Separate newest-ID watermark and older-catalog backfill cursor so updates and initial archive collection can progress together
- Three bounded attempts with exponential backoff for transient transport, timeout, HTTP 429, and HTTP 5xx failures

**Acceptance:** Can run a nightly sync that checks 50 collections across 5 sources in ~20 minutes, only fetches new posts since last sync.

---

### Phase 5: Tag Explorer (Weeks 14–16)

**Goal:** Browse-before-download exploration.

**Deliverables:**
- Tag Explorer view (search bar, provider selector, results grid)
- API endpoint for provider search without download
- Thumbnail loading from provider URLs
- Selection UI (checkboxes on grid)
- Detail modal from explorer (shows remote post, not local)
- "Add to collection" and "Create collection from selection" actions
- Download job queue for selected posts
- Progress tracking and notifications

**Acceptance:** Can search `blue_hair 1girl` on Danbooru, browse 200 results without downloading, select 30, create a new collection and add them.

---

### Phase 6: Pixiv and Additional Sources (Weeks 17–18)

**Goal:** Add Pixiv, ArtStation, Twitter/X, and Pawchive via gallery-dl while preserving the same original-media and folder-isolation guarantees as booru sources.

**Deliverables:**
- Pixiv provider implementation (gallery-dl wrapper)
- OAuth setup flow (guided `gallery-dl oauth:pixiv`)
- Pixiv authentication status check and display
- Ugoira detection and handling (skip or convert)
- Pixiv-specific metadata mapping (tags, bookmarks, user info)
- Test Pixiv sync with cursor tracking
- ArtStation profile/project extraction
- ArtStation finished-work selection: choose at most one original media asset per project, match its portfolio cover to the corresponding full-resolution asset, and exclude process images; a selected motion asset may yield three training frames
- For an ArtStation-hosted video, use the cover only to visually select one original-resolution frame, then select two other separated frames; use the attached still for Marmoset or external embeds and skip ambiguous projects rather than importing a thumbnail
- When ArtStation's detail API is reset or challenged, fall back to one persistent browser-like session on the artist portfolio host, open each project, visually crop-match the cover against the real project assets, and download the selected real asset through its highest available CDN tier; the cover is reference-only and an unreachable or ambiguous project fails without importing a thumbnail or advancing the cursor
- Twitter/X browser-cookie session configuration and media-timeline extraction
- Pawchive creator/post URL extraction and capability reporting
- Per-folder, per-source profile URL/ID/handle overrides
- Multi-asset identities for providers that retain every asset, ArtStation cover-selected project identities, secret-redacted metadata, and newest-plus-backfill offset cursors
- Stable end-of-feed handling across Pixiv, Twitter/X, Pawchive, ArtStation, Danbooru, Gelbooru, and e621; ArtStation must scan past process assets and Gelbooru cursors must remain stable when the requested page size changes

**Acceptance:** Can configure Pixiv and Twitter/X locally, scrape supported artist/profile sources, import every original asset in non-ArtStation multi-image posts, reduce an ArtStation project to its cover-matched high-quality finished work, handle ugoira/video/archive originals through the shared frame pipeline, and continue incremental backfill without exposing credentials.

---

### Phase 7: Filtering & QA (Weeks 19–20)

**Goal:** Filter control, dataset health checks.

**Deliverables:**
- Filter schema and UI editor (resolution, aspect, rating, tags, formats)
- Apply filters at fetch time (skip downloads that fail filters)
- Health dashboard (global metrics, issue detection)
- Issue types: low-res, extreme aspect, problematic formats, corrupt files, near-dupes
- Per-issue review flows (filtered galleries, batch actions)
- "Re-apply filters" action for existing images

**Acceptance:** Can set min resolution to 1024px, sync a collection, confirm low-res posts are skipped; can view health dashboard and see flagged issues.

---

### Phase 8: Bulk Import & Polish (Weeks 21–22)

**Goal:** Artist list import, UI refinement, performance optimization.

**Deliverables:**
- Bulk artist list import flow (paste list → resolve → create collections)
- Artist name resolution against Danbooru/Pixiv
- Virtualized grids (react-window or react-virtuoso) for performance
- Thumbnail size controls (small/medium/large slider)
- Keyboard navigation in detail view (arrow keys, J/K)
- Rejected posts tracking (never re-fetch)
- Soft delete / trash folder
- UI polish: loading states, error boundaries, toast notifications

**Acceptance:** Can import a 100-artist list, resolve ambiguities, create collections in bulk, navigate smoothly through thousands of images.

---

### Phase 9: Testing & Deployment (Weeks 23–24)

**Goal:** Integration testing, documentation, deployment.

**Deliverables:**
- Integration tests for provider adapters
- End-to-end tests for major flows (create collection → sync → export)
- User documentation (README, setup guide, usage guide)
- Deployment script (Docker or standalone installer)
- Configuration management (library path, default filters, sync schedule)
- Logging and error reporting

**Acceptance:** App is stable, documented, and ready for daily use.

---

### Phase 10: Optional Remote Jupyter Compute, Storage & Training

**Goal:** Preserve the complete local workflow while allowing the same UI to use an authorized Jupyter-hosted backend whose library lives under a configured server directory such as `/home/jovyan/artist-library`.

**Architecture:** The selected backend is the data and execution authority. It owns SQLite, images, sidecars, thumbnails, provider credentials, scraping, decoding, QA, exports, and training processes. The laptop browser communicates through an authenticated Jupyter proxy or SSH tunnel. A laptop backend must not proxy bulk image bytes to the server, and SQLite must not be opened across a network mount.

**Deliverables:**
- Selectable and documented local and remote deployment profiles using the same backend code
- Configurable library/database root and frontend API/static origin
- Authenticated tunnel/proxy guidance and server capability/health reporting
- Backend-owned credential uploads suitable for remote execution
- Durable reconnectable remote scraping, processing, and training jobs
- CPU/RAM/storage telemetry plus NVIDIA GPU discovery and selectable training devices
- Training configuration, launch, logs, cancellation, checkpoints, and artifact browsing
- Backup, restore, and explicit dataset transfer without changing folder semantics

**Acceptance:** Local mode still works without configuration. In remote mode, closing the laptop UI does not stop durable work; downloaded originals and processed datasets never need to reside on the laptop; reconnecting shows the same job/library state; and the backend is protected by the Jupyter/SSH access layer rather than exposed publicly.

## Technical Decisions & Tradeoffs

### Why SQLite?

**Pros:**
- Zero configuration, serverless
- ACID transactions, robust for metadata integrity
- Fast enough for 10k–100k images
- Easy backup (single file)
- Full-text search via FTS5 for tag queries
- JSON support for flexible metadata storage

**Cons:**
- No horizontal scaling (not needed for personal use)
- Concurrent writes are serialized (acceptable for single-user app)

**Decision:** SQLite is the right choice for a local, single-user dataset builder. PostgreSQL would be overkill.

### Why FastAPI + React instead of a desktop framework?

**Pros:**
- Web UI is more flexible for complex layouts (virtualized grids, drag-drop)
- Easier to add remote access later (run on a server, access from any device)
- Richer ecosystem for image viewers, galleries, charts
- Better separation of concerns (backend can be tested independently)

**Cons:**
- Requires browser, not a standalone .exe
- Slightly slower startup (need to launch server + browser)

**Decision:** The UI complexity (masonry grids, tag explorer, caption editor) benefits greatly from web tech. Users comfortable with ML tooling can run `python main.py` and open localhost.

**Alternative considered:** Tauri (Rust shell + web frontend) would give a native feel but adds build complexity and a handoff burden to Sonnet 5.

### Why wrap gallery-dl instead of reimplementing Pixiv scraping?

**Pros:**
- gallery-dl already solves Pixiv auth, which breaks frequently
- Handles ugoira, multi-page posts, edge cases
- Actively maintained (updates when Pixiv changes)
- Reduces maintenance burden by 80%

**Cons:**
- External dependency (must be installed)
- Subprocess overhead (slower than native)
- Less control over retry logic

**Decision:** Pixiv scraping is adversarial; offload it to a maintained tool. The user already has gallery-dl installed, so this is pragmatic.

### Why per-collection captions instead of global?

**Tradeoff:** More complexity in data model and UI, but critical for flexibility.

**Use case:** The same image of a blue-haired character by quasarcake should export as:
- In artist collection: `by quasarcake, 1girl, blue hair, long hair`
- In concept collection: `blue hair, 1girl, long hair` (no artist)

Ground-truth tags are image-level data and are edited directly. Provider metadata remains versioned provenance; no natural-language caption format is generated.

### Why not use Hydrus Network?

**Hydrus** is a mature tag-based media organizer with booru import support. Why build something new?

**Reasons:**
- Hydrus is a general media manager, not a training dataset builder
- No natural-language captioning; curated ground-truth sidecars remain trainer-ready
- Heavier, more complex (client/server, tags as first-class entities)
- No collection-based organization (everything in one pool)
- Learning curve is steep for users who just want "scrape artists, export for kohya"

**This app** is narrower in scope but purpose-built for the ML finetuning workflow.

### Why store images in a library instead of in-place?

**Alternative:** Let users point to existing folders, index in-place.

**Chosen approach:** App owns a library root, organizes images internally.

**Reasons:**
- Deduplication requires canonical storage (one file, multiple memberships)
- Simplifies thumbnail generation (known structure)
- Protects user from accidentally deleting source data
- Easier to back up (one directory)

**Tradeoff:** Requires disk space for a copy, but users building training datasets need organized storage anyway.

## Out of Scope (for now)

### Features explicitly deferred to v2 or later:

**1. Cara scraping**
- No public API
- Small, fragile target
- Ethical concern: Cara is explicitly anti-AI-training platform
- **Decision:** Out of scope. Users can manually download and import via file picker.

**2. CLIP embeddings & similarity clustering**
- Infrastructure for semantic dedup and map view
- Requires CLIP model download (~2GB), inference on all images
- **Decision:** Future optional work only. Phase 3 remains focused on deterministic tag curation and original-media still conversion.

**3. Automated captioning (VLM integration)**
- Running models like CogVLM, Qwen-VL, LLaVA to generate natural language captions
- Requires GPU, model downloads, prompt engineering
- **Decision:** Out of scope. This app manages tags only; users can generate prose captions externally if ever needed.

**7. Manual tagging UI**
- Adding, removing, or replacing tags by hand in the UI
- **Decision:** Implemented in Phase 3 with immutable source tags, editable overrides, selection-based batch add/remove/replace actions, sidecar synchronization, and undo.

**8. Multi-user / collaboration features**
- Shared libraries, permissions, conflict resolution
- **Decision:** Single-user only for v1.

**9. Cloud storage backends**
- S3, Google Drive, etc. for image storage
- **Decision:** Local filesystem only.

**10. Mobile app / responsive mobile UI**
- Complex grids and editors are desktop-first
- **Decision:** Desktop browsers only. Tablet might work but not optimized.

**11. Video/animation handling**
- The app does not store or train on video/animation files directly.
- **Decision:** Download original media and extract one representative frame. Pillow handles animated image formats, bundled FFmpeg handles common video containers, Python handles ZIP/CBZ, and Windows bsdtar handles RAR/CBR. Archives use a naturally ordered middle still frame and can fall back to a contained video when no still exists. The common trainer pipeline then caps the longest side at 2000px with OpenCV INTER_AREA and encodes non-JPEG frames as lossless WebP. Failed original extraction skips cleanly; provider thumbnails are never used as training substitutes. Multi-frame/keyframe dataset extraction remains out of scope.

**12. Advanced duplicate detection**
- Detecting cropped versions, heavy edits, style transfers
- Requires SSIM, feature matching, or CLIP
- **Decision:** pHash is good enough for v1. Defer to v2.

**13. Metadata versioning UI**
- Diffing tag changes between source record versions
- **Decision:** Data model supports it (version column), but UI to view diffs is v2.

**14. Artist opt-out / ethical scraping controls**
- Checking do-not-scrape lists, honoring artist requests
- **Decision:** Mention in docs, but no automated enforcement in v1. User responsibility.

**15. NSFW content filtering / age verification**
- Rating-based hide/blur for SFW environments
- **Decision:** User controls via rating filters. No special UI treatment.

---

### Why these are deferred:

The line between v1 and v2 is drawn at **"what delivers a complete, usable training dataset builder for booru + Pixiv sources."** Everything above is either:
- A nice-to-have enhancement (CLIP, VLM captions)
- A source with high maintenance cost vs. benefit (Twitter, Cara)
- A feature that users can work around externally (manual tagging, metadata diffs)

Users can start training models with v1. These features make the tool better but aren't blockers.
