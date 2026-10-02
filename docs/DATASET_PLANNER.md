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

### Character targets

Character targets are the characters the planner tries to give enough images. Either:

- **Fetch most-posted characters:** fetches each site's character tags ranked by post count, for example the top 3,000 on Danbooru and on e621. Danbooru's list also covers Gelbooru, which uses the same tag names. Placeholders such as e621's `fan_character` are skipped. A count of 0 leaves that site's current targets unchanged.
- **Upload a CSV** with `site` and `tag` columns, in priority order. This replaces all targets.

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

Downloading the selected posts into collections is not part of the planner yet.

## Resources

Harvested metadata takes roughly 0.5–1 KB per post on disk. Planning holds every artist's best candidates in memory at once (up to the **Candidates kept per artist** setting, default 300), roughly 1–2 KB per candidate. Plan very large lists on a machine with enough memory, or lower the candidate setting.
