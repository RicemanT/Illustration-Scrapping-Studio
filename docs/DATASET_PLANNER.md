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
2. **Drop small artists:** artists with fewer usable images than the minimum (default 20) are dropped. Parent/child variants of the same characters count once; children showing other characters (a cast of sprites under one parent) count separately.
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
3. **Remove off-style images in each collection's gallery.** Removal is recoverable through **Recover last deletion**. Styles mostly drift over time, so start with the era bar above a planner collection's gallery: it shows how many images were posted in each year. Click a year to keep images posted from then on; older images are dimmed and marked **pre-<year>**, **Select N older** selects them all (press **W** to delete), and the wildcards show only posts from the era unless you untick it. Sorting the gallery by **Posted (oldest)** or **Posted (newest)**, the posted year on each tile and the **posted from / to** year filters help too. To select many tiles at once, drag a box across them with the left or right mouse button (on a touchpad: double-tap and drag); everything the box touches is added to the selection, holding **Alt** or **Ctrl** while dragging removes it instead, dragging to the top or bottom edge scrolls, and **Esc** cancels. A plain click still opens the image. Shift+click on a checkbox selects every tile between it and the last one you ticked. Both work in the wildcards too.
4. **Run the plan again when you want replacements.** Images you removed from planner collections become bans first, so neither the plan nor a later download brings them back. Download the new run to fill gaps, then use **Remove images no longer selected** if needed.
5. **Fill gaps from the wildcards.** Below the gallery of every planner collection, **Wildcards: not selected by the plan** lists the artist's other harvested posts, most favorited or newest first, with posts the plan's filters rejected or that you banned hidden unless you show them. It works like the gallery: Shift+click or the checkbox selects, **Q** selects the page, and clicking a tile opens a viewer with a larger preview and the post's tags (arrow keys move, **S** selects). **W**, or **Accept**, locks the selected posts in the planner and downloads them into the collection. **Q** and **W** act on the section under the mouse: in the gallery **W** deletes, in the wildcards it accepts. Removing an accepted image later bans its post, like any other removal.
6. **Mark quality.** In a planner collection's image viewer, the **Quality** buttons under the image details set two independent marks: **Q** masterpiece, **W** best quality, **E** low quality, and **A** very aesthetic, **S** aesthetic. Pressing a lit mark clears it and **D** resets the image to **Normal** (no quality tags), which every image starts as. Marks are saved as you press them, but they only enter the image's tags (and its `.txt` sidecar) when you accept the folder, placed after all other tags, for example `…, smile, masterpiece, very aesthetic`. Opening an image in the viewer counts as reviewing it; the folder header shows how many images you have not viewed yet, and the gallery's **Quality** filter finds images by mark, **Normal** or **Not viewed yet**. After the folder is accepted, its committed quality tags can also be searched like any other tag. To save time, let the post scores set the quality scale first (see [Quality tags from scores](#quality-tags-from-scores)) and only correct what you disagree with.
7. **Accept the folder.** The header shows the folder's images against the plan's target. When it looks right, **Accept folder** marks every image accepted (no longer pending) and locks exactly these posts: every later plan selects exactly this collection's images for the artist, whatever the target, and deleting or accepting images is turned off. **Reopen folder** sets the images back to pending; they stay locked, and removing one bans it. Quality marks can be changed again after reopening; the sidecars keep the previously accepted quality tags until you accept the folder again.
8. **Export the training layout last.** Repeats are recalculated from the images actually left in each folder, so an artist trimmed from 60 to 40 images gets more repeats and keeps about the same number of training samples.

### Batches

**Inputs → Limit planning → Plan in batches** grows the dataset a chunk at a time: tick the sites to plan (for example Danbooru and Gelbooru), set **Add the next** (say 500) and press **Enable batch**. Artists of those sites already in scope stay, the next ones in list order join, and every artist that already has a planner collection stays in too, on any site, so the pilot and earlier batches (frozen once accepted) keep counting towards the shared character and tag goals. Everyone else is left out of analysis, plans and downloads. Then analyse the enabled artists (7. Image analysis, scope "enabled artists"; only new posts are analysed), run the plan, and download it under a new group name prefix (for example `batch1`): artists who already have a folder keep it, so only new artists get folders in the new groups. Remove images no longer selected, tag, curate, accept, and press **Enable batch** again for the next chunk.

**Danbooru takedowns.** When Danbooru bans an artist, its API stops giving out those posts' files (and hashes), so the plan counts them as "no file" and drops the artist. **Inputs → Limit planning → Find Danbooru takedowns** lists Danbooru artists with at least half their harvested posts hidden; **move** (or **Move all**) creates a Gelbooru entry with the same tag, in scope exactly when the Danbooru one was, and retires the Danbooru entry (a later artists CSV import keeps it retired). Then harvest Gelbooru (the new entries wait as pending), analyse and plan as usual. Artists that already have planner collections are not listed.

### Image analysis

Section **7. Image analysis** on the Planner page lets the server's GPUs look at the images, so a plan can choose by style consistency, content and aesthetics instead of metadata alone, and your work becomes skimming flagged images.

**Setup (once).** Click **Install analysis packages** (PyTorch, transformers, onnxruntime-gpu and aesthetic-predictor v2.5 go into the app's Python; the DeepGHS classifiers run as plain ONNX models, because the imgutils package conflicts with the app's numpy). On an NVIDIA server it installs the PyTorch and onnxruntime builds matching the driver (the default PyPI builds of torch and of onnxruntime-gpu 1.27+ need a CUDA 13 driver and otherwise silently run on the CPU); if the panel says PyTorch or onnxruntime cannot use the GPUs, the button reads **Install packages for this GPU driver**, and analysis jobs refuse to start on the CPU while GPUs are set. DINOv3 is gated: accept its licence on [its Hugging Face page](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m) and save a read token. Then **Run model test**: it loads every enabled model on the chosen GPUs and scores four random images from your library (each scorer's mean and range, the device each model runs on, and the expected images per second); the first run downloads about 6 GB of models into the Hugging Face cache. A model that fails is listed and skipped; the rest still run.

**GPU use is capped.** Only the GPUs listed under **GPUs to use** (default `0,1`; list all four to spread the heat) are used. Each holds its own copy of every model and takes the next batch when it is free. After each batch a GPU rests so it is busy at most **Max GPU busy %** of the time (default 50%), and a GPU that reaches **Pause at °C** (default 80 °C) takes no batches until it is 5 °C cooler while the others carry on. **Resume** uses the current settings. The table shows each GPU's load, temperature (amber from 75 °C, red from 83 °C), memory and power while you watch. Each site downloads on its own threads (**Download pace** per site), so Danbooru, e621 and Gelbooru run side by side; downloads, not the GPUs, are normally the bottleneck. While a job runs, **Scores so far** shows each scorer's mean and range, and marks a scorer red if it gives every image the same value.

**What is analysed.** For each artist: the newest posts (default 200) and an even sample of older ones (default 100), plus everything already downloaded into the artist's collection. Only a medium-size sample (about 850 px) of each post is downloaded; it is analysed in memory and only the results are stored (`<planner>/analysis.db`): DINOv2-L and DINOv3-L style vectors (mean of the patch tokens of chosen transformer blocks, by default blocks 4-6, 10-14 and 20-24), waifu-scorer v3 and v4-beta, Naflex (SigLIP2), aesthetic-predictor v2.5 and DeepGHS dbaesthetic scores, and DeepGHS classifiers for polished/sketch/monochrome, illustration/comic/3D, photo, AI-generated and drawing-style era. Anzhc's score (a YOLO classifier trained on which 10% band of Danbooru scores an image reached, 10 = top 10%) is one more scorer; calibration also shows, per predicted band, how many of your kept and removed images fall in it, to pick a threshold you agree with. **Stop** finishes the current batch; **Resume** continues the job later. Posts that could not be downloaded are listed under **Why N posts could not be analysed**: counts per reason and site (rate limited, not found, timeouts, not an image …) and the latest examples with the files tried and a link to the post. Downloads wait out rate limits (the site's Retry-After, up to a minute between six tries), and **Start analysis** queues failed posts again. A post counts as analysed only when every ticked model has seen it, so ticking a model later (DINOv3, say) and pressing **Start analysis** runs just the posts missing it, keeping earlier scores. With **Keep downloaded samples** (on by default, about 150 KB per post in `<planner>/analysis-samples`), such runs read the kept files instead of downloading again; the panel shows their size and can delete them.

**How a plan uses it** (tick **Use image analysis** in 3. Plan):

- **Style.** The newest analysed posts (half the target, 20 to 40) set the centre of the artist's latest style; every post is measured by its distance to it with both DINO models (a robust z-score). Posts farther than **Off-style beyond** (2.5) are dropped; those beyond **Flag for review** (1.8) are kept but flagged. When the latest style gives fewer than half the target (or the minimum per artist), the artist's main career style — the majority of everything analysed — is used instead.
- **Content.** Sketches, monochrome, comic pages, 3D and photos are dropped only when they are a minority of the kept style: an artist who mostly draws comics or monochrome keeps them. **Content rules for** chooses which types this applies to; AI-generated is off by default, because artist lists usually avoid AI artists and its hits are then mostly false positives. Untick any type whose detector catches good images. Comic-type blocked tags (`comic`, `4koma` …) follow the same majority rule, with or without analysis.
- **Aesthetics.** The five scorers become dataset-wide percentiles and are averaged. Posts below **Drop the dataset's weakest** (0.2, the bottom 20%) are dropped; otherwise aesthetics count only *within* the artist, so no scorer's taste overrides an artist's style.
- **Near-duplicates.** Of two posts whose deep features are at least 0.97 similar, the better-scored one is kept.
- **Safety net.** The models never starve an artist: when fewer than half the target (or the minimum per artist) survive, the plan relaxes one rule at a time (near-duplicates, then style, then the aesthetic floor) until enough posts remain, and the relaxed posts are flagged for review instead of dropped. Image sets on blank backgrounds (character sprites, manga pages) look alike to the models and are the usual reason. The run summary counts these artists, and the review panel (4.) shows why each post was skipped.
- Style fit and the artist-relative aesthetic rank join the existing scoring (characters, concepts, novelty, rarity); their weights are **style** and **aesthetic** under Score weights. Artists without analysis are planned from metadata as before; the run summary says how many and which content was excluded.

**Tags.** Section 6 also sets **very aesthetic** (top 5% by default) and **aesthetic** (top 15%, including the very aesthetic share) from the scorer ensemble, alongside masterpiece and best quality from post scores. Like quality marks, hand-set aesthetic marks are never replaced, and the viewer offers **Use auto**.

**Review.** Planner collections open sorted **Flags first**. The strip above the gallery says which style was kept, which content was excluded or kept as the artist's style, and counts each flag; **Show only the flagged** filters to them. Tiles carry badges only for what the plan would drop (off-style, sketch, dup …), normally leftovers that **Remove images no longer selected** clears; softer hints (borderline style, possibly AI, among the artist's weakest) appear only in the viewer's analysis panel, and the image viewer's **Image analysis** panel shows the style distance, every scorer with its percentile, the classifiers and whether the plan would drop the image. All manual tools stay available.

**Calibration and the redo.** **Compare** measures how well each style model and block range, and each scorer, agrees with what you removed and marked by hand (AUC: 0.5 is chance, 1.0 perfect); **use blocks …** switches the planner to the best range. **Reset hand curation** writes every decision (removals, locks, bans, accepted folders, marks, eras) to `<planner>/exports/curation-backup-*.json` and clears them: removed images become choosable again (their delivery items are marked `removed`, so the next plan does not ban them), and the backup keeps them as calibration labels. Then analyse, plan with analysis, download into the same prefix, **Remove images no longer selected**, apply tags, and review flags-first.

**Bulk accept.** Section 5 has **Accept all collections** and **Reopen all**, which accept or reopen every planner collection at once (the same as each folder's Accept button). The quality and aesthetic tag pass skips accepted collections unless **Include accepted collections** is ticked, for example after pulling wildcards into folders you had already accepted; hand-set marks are never replaced either way.

### Captioning

Captions come from a captioning script kept outside this repository (it holds API keys and prompts), written as `<image>_nl.txt` next to each image, the file the caption editor shows. The script should walk the library's group folders recursively and read each image's `.txt` sidecar.

Each image's tags come from its `.txt` sidecar (the curated trainer tags), minus the quality and aesthetic marks, which are judgements rather than content. **8. Captioning → Build character facts** scans every harvested post and writes `<planner>/exports/character-facts.json`: for each character tag a clean name, its series (copyrights on at least a quarter of its posts) and its usual look (hair, eyes, ears, tails, horns, halos, e621 species and fur … on at least 40% of the posts showing that character alone). The script finds the character tags in each sidecar and adds their facts to the request, so the model names characters correctly without a web search. Rebuild the facts after harvesting more artists.

**Check captions** (also in 8.) verifies every caption in the planner collections against the prompt's rules, in code rather than with a model: it starts with the artist trigger exactly once, stays within the word range (200–350 by default) in one paragraph, avoids "or" outside quoted text, asterisks, brackets and "This image shows" phrasing, names every character its tags name (by the character facts' clean name), never uses raw tag spelling, quotes some text when the tags say the image has text, and is no refusal or prompt echo. Click a problem to list its images; **Set aside flagged captions** renames the ticked problems' captions to `…_nl.txt.flagged`, so the captioning script, which skips captioned images, writes them again on its next run.

### Tracker

**Tracker** (top bar) follows characters and general tags across every planner collection as one growing dataset: the pilot, then each batch of artists you plan and download into the same groups. Planner collections show a ✓ in the sidebar once accepted, each group shows how many are accepted, and **Hide accepted** leaves only the collections still to review; the Tracker's **Collections** card lists them too, with each one's images against the target.

For each character (and, on the second tab, each general tag) the tables show:

| Column | Meaning |
| --- | --- |
| Planned | Images the plans picked for the collections (the latest download into each collection) |
| Now | Images in the collections now |
| Accepted | Images in accepted collections |
| Goal | Your goal, or the planner's **character floor** for target characters (marked * when it is yours) |
| Gap | How many images are still missing to reach the goal |
| Spare | Usable harvested posts not in any collection yet, in collections still under review (number of artists in brackets); posts before a collection's era, filtered, banned or removed by hand are not counted |
| Status | **missing** (a target with no images), **lost in curation** (fewer than planned), **below goal**, **ok**; general tags only get a status against a goal you set |

Filter by status, series, priority series or target list, and click a row to see which collections contain the character, where curation lost it, and which collections' wildcards could add it; set your own goal there, for one character or every character of the series. Character counts update as you curate; general tags are recounted in the background (automatically when they are older than 30 minutes, or with **Recount now**).

In a planner collection, the **Characters** strip above the gallery lists the characters it contains with their dataset-wide counts against the goal (★ marks priority series); click one to show its images. **Lost vs. plan** lists characters the plan picked here that curation removed. Deleting images that leave a character short of its goal shows a short notice (longer and red for priority characters); it never blocks the deletion. In the wildcards, **Fills gaps** sorts the posts that add the most-needed characters first and labels what each would add.

**Releases.** When you train a release, save a snapshot under **Release snapshots** (for example `v0.4`). Then continue: rename the pilot groups if you like (for example to `Full danbooru`), plan the next batch of artists and download it with the same group prefix (`Full`), so the new artists' collections join the same groups and the accepted ones stay as they are. **Compare** with a snapshot adds a Δ column showing what the batch changed, and every table and snapshot downloads as CSV.

### Quality tags from scores

Section **6. Quality tags from scores** on the Planner page, or **Auto quality from scores** in a planner collection's header, sets the quality scale (masterpiece, best quality, low quality) from each post's score on its site. Raw scores are not comparable across years or ratings: newer posts reach more users and explicit posts collect more votes. So each post is ranked only against the harvested posts from the same site, year and rating:

| Setting | Default | Meaning |
| --- | --- | --- |
| Rank by | Score | Score (up minus down votes) or favorites. Danbooru, e621 and Gelbooru all provide a score; ranking is always within one site. |
| Masterpiece: top % | 5 | Posts in the top 5% of their bucket. |
| Best quality: top % | 10 | The next posts up to the top 10% (the share includes the masterpiece share). |
| Low quality: bottom % | 0 | Off by default: a low score can also mean few people saw a post. |
| Smallest bucket | 300 | A site/year/rating bucket with fewer harvested posts uses the site's whole year, then the whole site. |
| Separate years / ratings | on | Turn off to rank within the whole site instead. |

A post's share counts every harvested post with at least its score, so tied scores never straddle a cut-off: in a bucket where most posts score 0, none of them gets a tag. **Cut-offs per year and rating** shows the resulting score thresholds per site, year and rating, with the number of posts behind each.

The population is every post the harvest collected, so the percentiles describe your artist list rather than the whole site; popular artists naturally receive more masterpiece marks. The first run counts the scores once (a few minutes for millions of posts); tick **Recount scores first** after harvesting more. Changing the settings and applying again updates the automatic marks.

Automatic marks never replace a mark you set by hand: pressing **Q**, **W**, **E** or **D** in the viewer makes the image's quality yours (**A** and **S** only touch the aesthetic scale, which stays manual). The viewer explains each suggestion, for example *Auto: masterpiece · Danbooru score 410, top 0.75% of 671 2025 sensitive posts*, and offers **Use auto** when your mark differs. Accepted collections are skipped. The gallery's **Quality** filter has **Auto-assigned** and **Set by hand**, and like hand marks the tags reach the sidecars when you accept the collection.

### Captions

Natural-language captions stay where your captioning script writes them, next to each image and its tag sidecar: by default `<image name>_nl.txt` (change the ending in **Settings → Caption files**, for example to `.caption`). Point the script at the collection folders under `<library>/images/` directly; nothing has to be moved.

The image viewer shows the caption under the ground-truth tags. Edit it in place: **Ctrl+S** saves and **Ctrl+Enter** saves and opens the next image, so you can review a folder quickly. Leaving with unsaved edits asks first. Before every save or delete, the previous version is copied to `<library>/.trash/captions/`, and if the file changed on disk after you opened it (for example, a captioning run rewrote it), saving is refused so nothing is overwritten by accident. You can also type a caption for an image that has none.

Gallery tiles with a caption show **NL**, and the **Caption** filter finds images with or without one, for example to see what a captioning run missed. Caption files move with their image when it is removed (and come back with **Recover last deletion**), are deleted with it, follow the kept copy when duplicates are merged, and are copied by dataset exports. Quality tags and other ground-truth edits never touch them.

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
