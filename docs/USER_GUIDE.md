# User guide

## First run

Follow [Installation](INSTALLATION.md) or [JupyterHub](JUPYTER.md). Open **Settings** to check the active library path, processing preferences, worker count and provider credentials. Everything you scrape and configure belongs to that backend. A laptop instance and a server instance have separate libraries unless you explicitly migrate one using backup/restore.

## Collections and searches

Click **+ New Collection**, choose a type, then enter a display name, search and sources.

- **Artist**: enter an artist identity; normal names may contain spaces. Existing artist normalization and optional `Drawn by {artist}` formatting apply. The template must contain `{artist}`. Provider-specific searches can override the default.
- **Character**: enter one character identity, such as `hatsune miku`, which becomes `hatsune_miku`. It is a search identity, not an automatic artist caption.
- **Tag / query**: enter the provider's search expression, such as `landscape sunset -monochrome`. Use underscores within a multiword tag. Operators and search limits vary by provider; the app does not translate every provider's syntax.

The display name does not need to match the query. Type is fixed after creation to protect caption semantics. Artist and character collections with the same name can coexist in a group. Every collection owns separate files and metadata; importing an identical image into another collection does not make them share a writable original.

In **Sources**, enable or disable any compatible site, including sites not chosen at creation. Disabling keeps existing images, provider-specific queries and sync cursors. Providers needing credentials can be selected in advance; finish setup in Settings before scraping. Source edits wait until active scraping/import jobs finish.

Edit searches in **Sources**. Search changes reset provider pagination and are blocked while affected jobs are active. Local gallery filters affect the view, not future downloads. Changing settings does not rewrite existing images or force deduplicated sources to download again.

## Providers and credentials

Danbooru, Gelbooru, e621 and yande.re support the three collection types. Gallery-dl-backed ArtStation, Pixiv, Twitter/X and Pawchive integrations use artist/profile searches; they do not support arbitrary character/tag collections.

Configure providers in **Settings**. Gelbooru uses a user ID and API key. Other adapters may require their own login/token/cookies; availability can change. Use the provider's status/test controls and read the resulting diagnostics. Uploaded Twitter cookies are a Netscape-format `cookies.txt` file from your own session. On a remote instance, laptop browser profiles are not available to the server: use the supported upload/configuration controls.

Saved secrets are local backend files, never repository content. They are not transferred merely by cloning the repository. Backups include them, so keep archives private. A provider denial may reflect expired credentials, account permissions or site restrictions; a 403 alone does not establish which cause applies.

## Groups and bulk lists

Open **Groups / bulk lists**. Create a group and choose its batch provider. Select Artists, Characters or Tags / queries and upload/paste a UTF-8 text list, one identity or full query per line. For example, a character list can contain:

```text
hatsune_miku
hakurei_reimu
```

Choose multiple **Sources for new collections** if wanted; the group batch provider is included automatically. Additional sources apply to newly created collections; existing entries remain unchanged. Preview first, inspect invalid/existing entries, then choose **Create collections**. Blank and duplicate entries are skipped; types deduplicate independently. Limits are 5,000 lines and 1 MiB per list. A group with a profile-only provider cannot create character/tag searches.

Choose **Scrape group** to run that group's enabled collections with its designated provider. **Sync All** targets enabled sources across groups. Groups are actual parent directories: `images/<group-slug>/<collection-slug>/`, with matching thumbnails. Ungrouped collections use their own direct directory. **Move into group** moves files with membership; finish conflicting jobs first. Group renaming/deletion and nested groups are not exposed in the current UI.

## Scrape and import

Use **Sync now** for a source, **Scrape group** for a group, or **Sync All**. Configure amount, ordering and optional dates. Requested posts are not a guaranteed number of new images: source duplicates, quality checks, unavailable files and extracted frames affect totals. Provider request throttles remain active independently of worker count.

The **import** tab offers provider previews, Have/New filtering, selection and explicit import. Local gallery filtering does not change the remote search. Browser disconnection does not cancel backend jobs. Job history and progress remain available when you reconnect; backend restarts use the existing job recovery rules. Cancel drains current native work and may take time on a large file.

Scheduled Sync All is optional in Settings. Its scope is every enabled source, so check it before leaving an instance unattended.

## Gallery and image review

Thumbnails preserve image proportions. Adjust thumbnail size and sorting, use local tag/provider/rating/review/favorite/dimension filters, and select images for supported batch actions. Counts come from the backend; requested download amounts are not used as image totals.

Open an image for fit/actual-size viewing, source history, tags and file locations. Copyable paths refer to the active backend's filesystem. The viewer supports previous/next within the gallery page and warns about unsaved tag edits. Deletion recovery is available for the most recent supported image deletion batch. Deleting an entire collection is permanent, including its recovery snapshots.

The **duplicates** tab shows folder-local candidates. Inspect before resolving near matches. See [Deduplication](DEDUPLICATION.md) for exact and perceptual behavior.

## Tags and sidecars

Each image has a `.txt` sidecar derived from curated tags and configured category policy. Global category switches can be overridden per collection. Source metadata remains recorded separately. Artist-format triggers apply only to artist collections; character/tag collection names are not inserted as artist credits.

Use the tags view and image editor for supported edits. Some tag/category changes immediately rewrite affected sidecars, so review their scope. Local include/exclude tag filters only change the view. No natural-language caption model is included.

## Image processing

**Settings > Import processing** controls future jobs:

- Enable/disable resizing and re-encoding. Disabled preserves original still-image bytes; videos/animations/archives still require still-frame extraction.
- Set a maximum longest side, or leave it blank for no resizing. Images are never upscaled.
- Choose AREA, Lanczos, cubic or linear resizing.
- Choose automatic format, WebP, JPEG or PNG; customize WebP lossless/quality/method and JPEG quality.

Defaults retain the prior 2000-pixel AREA policy: JPEG stays JPEG, other supported stills become lossless WebP. Quality admission checks are separate from processing. Existing records without a stored processing policy use legacy QA rules. A new job snapshots its processing policy; changing Settings does not modify an already running job or existing images.

## Workers and storage

Worker count accepts any positive integer, with no fixed application maximum; default is two. Higher counts consume more CPU/RAM and may not improve provider-limited throughput. Configure according to actual workload, not just the host's logical CPU count. Booru adapters currently space request starts one second apart; fast files may finish before another request starts even with many workers. Waiting workers are shown separately from transfer/processing activity. Danbooru documents a 10 requests/second read burst limit but asks long-running clients to stay around 1 request/second; see its [API guidance](https://safebooru.donmai.us/wiki_pages/help%3Aapi). This is API guidance, not a guarantee about media CDN limits. The app retains its conservative request-start pacing.

**Active server and storage** reports the backend's library and available disk space. The default free-space reserve is 5 GiB. Below it, new download/ingestion admission fails with an explanation. This is a soft check, not a hard disk quota: in-flight downloads and other processes may cross the threshold. Free space or change the reserve, then rerun failed work. Filesystem capacity may differ from an account quota.

## Dataset checks and exports

The **dataset** tab scans stored image/sidecar pairs and reports missing files, thumbnails, metadata and other quality issues. Select repairable issues explicitly; repair rebuilds supported thumbnails/sidecars, not original pixels. Interrupted scans can resume remaining images where offered. Startup does not silently repair all sidecars.

Export a validated selection using copy or hardlink mode. Hardlinks may fall back to copies; do not externally modify hardlinked files, since linked paths refer to the same content. Exports preserve a manifest and provenance. Export is not a complete application backup: use the backup command for the database, recovery history and settings.

## Logs and maintenance

Open **Logs** or an error notification's diagnostics link. Filter by request/job and expand the explanation. Confirmed observations and likely causes are distinguished. Unknown errors remain unknown. History rotates and is not permanent; export a relevant page when reporting an issue. Check exported logs for private paths, names and provider information before sharing.

See [Troubleshooting](TROUBLESHOOTING.md), and follow [Installation](INSTALLATION.md#back-up-the-library) for backup/restore and safe updates. The app does not launch training jobs or manage GPUs/models.

For complete provider authentication instructions, follow [Provider setup](PROVIDER_SETUP.md), including Windows/Jupyter commands and cookie upload.
