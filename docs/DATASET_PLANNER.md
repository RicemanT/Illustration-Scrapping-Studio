# Dataset planner

The planner chooses which posts to download for a large artist dataset. It works from post metadata (tags, favorites, scores, sizes and dates), so you can plan hundreds or thousands of artists before downloading any images. Open it from **Dataset planner** in the sidebar.

The planner keeps its own database and never creates, changes or deletes your collections, images or sidecars. Its data lives in a `planner` folder inside the active library, `<library>/planner/`. The Planner page shows the exact location. To store planner data elsewhere, set the `ARTIST_PLANNER_PATH` environment variable before starting the backend.

## 1. Inputs

### Artists

Upload a CSV with one row per artist:

| Column | Required | Meaning |
| --- | --- | --- |
| `site` | yes | `danbooru`, `gelbooru` or `e621` |
| `query_tag` | yes | The exact artist tag on that site; spaces are converted to underscores |
| `display_name` | no | Name shown in the planner and written to the manifest; defaults to the tag |
| `tag_id`, `post_count` | no | Kept for your reference |

```csv
site,query_tag,display_name
danbooru,example_artist,example artist
e621,another_artist,
```

Uploading again updates existing artists and adds new ones. Artists missing from the new file are disabled, not deleted; their harvested data is kept and they return if you list them again.

### Limiting planning to some artists

To try settings on a small group first (a pilot), open **Limit planning to some artists**, enter one artist per line (the tag or display name, optionally with a site first, such as `e621,some_artist`) and choose **Enable only these**. The other artists stay on the list but are left out of harvests, plans and downloads. **Enable all listed artists** brings everyone back. Artists you removed from the CSV stay out.

### Character targets

Character targets are the characters the planner tries to give enough images. Either:

- **Fetch most-posted characters:** fetches each site's character tags ranked by post count, for example the top 3,000 on Danbooru and on e621. Danbooru's list also covers Gelbooru, which uses the same tag names. Placeholders such as e621's `fan_character` are skipped. A count of 0 leaves that site's current targets unchanged.
- **Upload a CSV** with `site` and `tag` columns, in priority order. This replaces all targets.

### Priority series

To favor the characters of particular series, for example games you want the model to know well, enter their series (copyright) tags under **Priority series**, separately for Danbooru/Gelbooru and e621, and choose **Add as priority**. Series names differ between the sites, for example `sonic_(series)` on Danbooru and `sonic_the_hedgehog_(series)` on e621. Each tag is checked first: renamed tags follow their alias, and tags that are not series tags on that site are reported and skipped.

- **Danbooru:** characters whose posts mostly belong to the series (related tags with an overlap of at least 0.5; the site returns at most 500), plus every tag named `<character>_(<series>)`, and `<character>_(<base>)` for `<base>_(series)` tags such as `fate_(series)`.
- **e621:** characters whose tags imply the series, following sub-series (`twilight_sparkle_(mlp)` → `friendship_is_magic` → `my_little_pony`). Few anime characters on e621 imply their series, so anime series add little there.

Only characters with at least the chosen number of posts are added (default 30). Large lists add many targets, so raise the minimum to keep priority meaningful: thirty popular series added about 7,500 Danbooru characters at 30 posts but about 3,000 at 300. The lookup runs in the background (large e621 series such as `disney` take a few minutes) and shows its progress.

Priority characters are topped up before other characters, and their character need is multiplied by the **priority character** weight (default 1.5). Each artist's image count does not change, so artist balance is preserved; what shifts is which images artists who draw these series contribute. Refreshing the most-posted list keeps priority characters. **Clear priority** unmarks them and removes the ones that were only added as priority.

## 2. Harvest metadata

The harvest pages through each enabled artist's posts, newest first, up to the per-artist limit (default 2,000). No images are downloaded. Each site runs at the app's normal pace of one request per second, and the three sites run in parallel. A page holds up to 200 Danbooru posts, 320 e621 posts or 100 Gelbooru posts, so harvest time grows with the size of your list: about 1,000 artists typically take under an hour.

Progress is saved after every page. Stopping, closing the browser or restarting the backend never loses finished work: **Start / resume harvest** continues each artist from where it stopped and retries artists that failed. **Refresh finished artists** harvests everything again to pick up new posts.

Gelbooru needs a User ID and API key in Settings. Some posts have no downloadable file for anonymous API users; they are skipped as `no file`.

## 3. Plan

**Run plan** selects images for every enabled artist and stores the result as a numbered run. Earlier runs are kept.

1. **Skip unusable posts:**
   - file formats the import pipeline cannot process;
   - images below the minimum short side (default 768 px) or beyond the aspect-ratio limit;
   - posts with a blocked tag;
   - posts crediting more artists than the limit (default 2);
   - optionally, excluded ratings, posts before a chosen year, or all motion posts.

   Videos, animations and ugoira are included by default; when imported, each produces up to three frames.
2. **Drop small artists:** artists with fewer usable images than the minimum (default 20) are dropped. Parent/child variants count once.
3. **Pick images in turns.** Each artist picks one image per round until it reaches the per-artist maximum (default 60). Each pick goes to the image that adds the most:
   - **Quality:** favorites (or score) ranked within the artist, so a small artist's best images rank as highly as a popular artist's.
   - **Novelty:** content the artist's selection does not cover yet. This keeps an artist tag from being tied to one character.
   - **Character need:** the image shows a target character that is still below its target count across all artists.
   - **Rarity:** the artist's images with unusual tags.
   - **Boost:** the image has a tag from the boost list.

   No single character may take more than 30% of an artist's images unless the artist has nothing else.
4. **Top up characters:** target characters still below their target (default 30 images) receive extra unselected images from listed artists, at most 10 per artist.
5. **Set repeats:** each artist gets about the same number of training samples per pass (default 200). Artists with fewer images get more repeats, up to the maximum.

The run summary shows kept and dropped artists, image and sample totals, why posts were skipped, and which characters remain below target. Characters still short need images from artists outside your list, for example through Character collections.

### Multi-artist posts

Danbooru and e621 list a post's credited artists alphabetically, not by role, so the planner cannot tell a main artist from a colorist or collaborator. A post within the credit limit is kept for each listed artist who is in your list. The manifest records every credited artist, so captions can name all of them. Posts with more credits, typically large group collaborations, are skipped.

### Tag lists

Danbooru and e621 use different tag names, so each has its own blocked and boost lists; Gelbooru uses the Danbooru lists.

- **Blocked tags** skip a post. The defaults cover comics, text-only pages, AI-generated work (Danbooru; e621 no longer accepts it), photos, low-resolution posts, upscales and compression artifacts.
- **Boost tags** favor useful concepts that are rare in typical training data: unusual camera angles and composition, action, interaction between characters, crowds, environments, vehicles, weather, lighting and effects. Each default boost tag appears on less than about 2% of its site's posts.

The defaults were checked against the live Danbooru and e621 tag databases on 2026-10-02; every tag exists under its current name. Sites rename and retire tags over time, so **Check on Danbooru** and **Check on e621** compare a list with the live site. The check reports renamed tags (with **Use current names**) and deprecated, empty or missing tags (with **Remove unusable**).

### Weights

Defaults were measured on live Danbooru and e621 data. Heavier character and boost weights replaced too many of an artist's best original works with fan art. Change the weights under **Score weights and tag lists**. **Load this run's settings** restores an earlier run's configuration.

## 4. Review

Choose a run and an artist to see the selected images and the most popular posts that were not selected. Hover over a thumbnail for its characters, size, rating and score breakdown; click it to open the post on its site.

- **Lock** always includes the image, even if a filter would skip it.
- **Ban** never includes it.
- **Clear** removes your decision.

Locks and bans take effect on the next plan run. Thumbnails are fetched by the backend, because Danbooru's image server rejects direct browser requests, and cached in `<planner data>/thumbs`.

## 5. Export

**Export manifest** writes `<planner data>/exports/run-<id>/`:

| File | Contents |
| --- | --- |
| `manifest.jsonl` | One line per selected post: site, post ID, artist tag and name, all credited artists, role (`selected`, `locked` or `character_topup`), repeats, md5, file URL and extension, whether it is a motion post, size, rating, characters, copyrights and score breakdown |
| `selected_<site>_ids.txt` | Selected post IDs for each site |
| `summary.json` | The run's settings and summary |

## 6. Download

**Download run** downloads a completed run's selected posts into collections:

- Each site gets a group named `<prefix> <site>` (default prefix `Planner`), and each artist an artist collection whose source query is the exact site tag. Existing planner collections are reused. If a group with that name exists for a different site, choose another prefix.
- Downloads go through the normal import pipeline: original-quality files, the collection's quality floor, duplicate checks, sidecars with the `Drawn by <artist>` trigger, and up to three frames from each video, animation or ugoira.
- Planner collections are **protected**, so Sync All, scheduled syncs and group scrapes skip them. Without this, a scheduled sync would scrape each artist's entire gallery. You can still sync a single collection manually.
- Fresh metadata is fetched 100 posts per request on Danbooru and e621 (one at a time on Gelbooru). Downloads then use the app's normal request pace and worker count, so tens of thousands of posts take days. Check free disk space first; Settings shows it.

Progress per site counts posts as **done** (new images added), **skipped** (already in the collection), **filtered** (below the collection's quality floor), **missing** (deleted from the site since the harvest) and **error**. **Stop** keeps finished downloads; **Resume** continues the rest and retries errors, also after a backend restart. Downloading the same run again reuses its collections and skips posts already present. Do not move or delete planner collections while a download runs.

### Curating by hand

The planner balances content, not style: it only sees tags, favorites and sizes, and its novelty score favors variety, which can pull in sketches, traditional-media pages or an artist's older style. Judge style yourself:

1. **Narrow the candidates first.** **Only each artist's newest posts** (for example 100) keeps the plan within each artist's recent work, where style is most consistent. Planning still balances characters and content inside that window.
2. **Plan a little more than you need**, for example 90 images per artist if you want about 60, and download the run.
3. **Remove off-style images in each collection's gallery.** Removal is recoverable through **Recover last deletion**.
4. **Run the plan again when you want replacements.** Images you removed from planner collections become bans first, so neither the plan nor a later download brings them back. Download the new run to fill gaps, then use **Remove images no longer selected** if needed.
5. **Export the training layout last.** Repeats are recalculated from the images actually left in each folder, so an artist trimmed from 60 to 40 images gets more repeats and keeps about the same number of training samples.

### Removing images a run no longer selects

Downloading another run adds its new images but keeps everything already downloaded, including posts you have since banned. **Remove images no longer selected…** first shows how many planner images in the run's collections the run does not select, then removes them on confirmation, so the training folders match the plan. Only images an earlier planner download added are considered; images you imported yourself and other providers' images are never removed. Removed images go to the collection's recovery, which keeps only the latest removal per collection, so this replaces any earlier **Recover last deletion** batch there.

### Checking styles with a GPU

`tools/planner_style_check.py` flags downloaded images that sit far from their artist's usual style, such as sketches, photos, 3D renders or guest art. It reduces each image to style statistics from a standard VGG16 network (the mean and spread of its feature maps at four depths) and flags images whose distance from the artist's median style is unusually large (robust z-score above 3.5 by default). Artists with fewer than 8 downloaded images are skipped.

The app does not need PyTorch. Run the script in any Python environment that has torch, torchvision, numpy and Pillow, while no planner download is running:

```bash
python tools/planner_style_check.py --library /path/to/library --pause 0.1
```

It downloads the VGG16 weights once. On one GPU it handles several hundred images a second; `--pause` waits between batches to keep a shared GPU cool, `--device` picks the GPU (`cuda:1`, or `cpu`), and `--threshold` changes how unusual an image must be. Results go to `<planner data>/exports/style-check-download-<id>.csv`, and flagged images show an **Off-style** badge in Review; **Only artists with style flags** finds them. Ban the ones you agree with, or rerun with `--ban` to ban every flagged post (locked posts are skipped). Then run the plan again, download it, and remove images no longer selected.

Treat flags as suggestions. These statistics are dominated by colour, so an artist's rare light or pastel piece can be flagged while a screenshot with drawing-app interface in it is not. `--grayscale` judges line work, shading and texture without colour; compare both on a pilot before relying on either.

### Training layout

**Export training layout** writes `<planner data>/exports/run-<id>/training/`:

| File | Contents |
| --- | --- |
| `dataset.toml` | One `[[directory]]` block per collection with its path and `num_repeats`, for a diffusion-pipe dataset config; add your resolution and bucket settings there. Repeats are `Training samples per artist ÷ images in the folder now` (at most **Repeats at most**), so they follow hand curation |
| `folders.json`, `folders.csv` | Site, artist, tag, collection, path, image count and repeats, for other trainers |

Paths are on the machine running the backend. A video post can contribute up to three images, so image counts can exceed the planned post counts.

## Saving disk space

Downloaded images follow **Settings → Processing**. For a large dataset, a lower **Max dimension** (for example 1536 instead of 2000), JPEG quality around 95 instead of 100, and lossy WebP (lossless off, quality around 90) for PNG sources roughly halve storage compared with the defaults. Settings apply to images downloaded afterwards; images already downloaded keep their format.

## Resources

Harvested metadata takes about 2 KB per post on disk (a real harvest measured 1 GB for 536,000 posts), so millions of posts need tens of gigabytes. Planning reads the whole post table twice per run, which takes minutes on a hard disk at that size. Planning holds every artist's best candidates in memory at once (up to the **Candidates kept per artist** setting, default 300), roughly 1–2 KB per candidate. Plan very large lists on a machine with enough memory, or lower the candidate setting.
