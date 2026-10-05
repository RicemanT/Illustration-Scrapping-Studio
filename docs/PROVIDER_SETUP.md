# Provider setup: local and Jupyter

Complete these steps in the Settings page of the instance you want to use. If the app runs on Jupyter, credentials must be saved in that server-hosted app. Cloning GitHub does not copy your laptop credentials. You normally do not need to restart the backend after saving credentials.

## Before starting

1. Open **Settings** and confirm **Active server and storage** shows the intended library.
2. Check that the gallery-dl runtime reports ready. If not, run the app's setup using its virtual environment and restart.
3. Choose the provider below. A configured/ready indicator is not proof of successful live access; test a small import afterward and inspect Logs if it fails.

| Provider | Setup in this app |
| --- | --- |
| Danbooru, e621 | No additional credential fields currently exposed; availability/search restrictions depend on the site. |
| Gelbooru | Numeric user ID and API key |
| DeviantArt | Public access; optional OAuth refresh token and custom API client |
| Pixiv | Final OAuth refresh token |
| Twitter / X | Signed-in cookies.txt upload recommended, especially for Jupyter |
| ArtStation, Pawchive | No additional login controls for currently supported public sources; gallery-dl must be ready |

## Gelbooru

1. Sign in to your own Gelbooru account in your browser.
2. Open **Account > Options** and locate your numeric User ID and API key.
3. Paste them into **Settings > Gelbooru API** in the app.
4. Click **Save and test**. For already saved credentials, use **Test saved credentials**.
5. A successful test displays a connection-success message. The key input stays blank after saving; that does not mean the saved key was erased.

If the test fails, inspect the error and correlated Logs. Confirm the account credentials, network reachability and provider response before retrying. Do not publish the API key.

## Danbooru and e621 accounts (optional)

Both sites work without an account. In Settings → **Danbooru and e621 access, request pace**, you can add a username and API key; **Check and save** verifies them with the site first.

- **Danbooru:** create an API key on your profile page. An account does not raise read limits (10 requests per second per IP at most, about 1 per second asked for long sessions), but requests then carry your user ID in the User-Agent, which is how Danbooru asks bots to identify themselves.
- **e621:** create an API key under Account → Manage API Access. e621 hides the file link of some posts from visitors who are not logged in; an account may unlock them. Requests then name your username, as e621 asks.

Keys are only sent to the sites' API, never to their file servers, and are never returned by the app's API.

## Request pace

The same section sets, per site, the seconds between API requests (searches and post lookups) and between image downloads. Pacing is shared by every job on the same site, and changes apply to running jobs within a second.

| Site | Documented API limit | Fastest API setting allowed |
| --- | --- | --- |
| Danbooru | 10 requests/s per IP; about 1/s for long sessions | 0.15 s |
| e621 | 2 requests/s; faster returns HTTP 503 | 0.55 s |
| Gelbooru | not published; it throttles heavy use | 0.25 s |

Images come from the sites' file servers, which these limits do not describe and which tools such as gallery-dl do not throttle. The default waits 0.2 s between download starts; downloads also run in parallel up to the worker count, so on a server with many cores raise the worker count too. **Fast** sets Danbooru and Gelbooru to 2 API requests per second, e621 to about 1.3, and downloads to 0.05 s.

If a site answers "too many requests" (HTTP 429, or 503 from e621), the app waits as the site asks, doubles that site's interval and returns to normal over the following successful requests. Failed downloads are retried up to three times before they are reported.

## Pixiv

Use a terminal, not a notebook `!` command, because the OAuth flow asks for interactive input. Open the terminal in the project directory containing `studio.py`.

**Windows laptop (PowerShell or Command Prompt):**

```powershell
.\.venv\Scripts\gallery-dl.exe oauth:pixiv
```

**Jupyter/Linux terminal:**

```bash
# Replace this path if you cloned elsewhere.
cd /home/jovyan/Illustration-Scrapping-Studio
.venv/bin/gallery-dl oauth:pixiv
```

1. The command prints a login URL and browser instructions. Open that URL in your laptop browser, even if the command runs on Jupyter.
2. Follow the command's Developer Tools / Network instructions while logging into your own Pixiv account. The callback code can appear in a redirect even when its destination page does not load.
3. Copy the short-lived callback `code` requested by the command and paste it into the terminal prompt. At this point authentication is not finished yet.
4. Wait for gallery-dl to exchange the code and print the final **refresh-token**.
5. Paste only that final token into **Settings > Pixiv OAuth > Final Pixiv refresh-token value**, then click **Save token**.
6. Try a small Pixiv artist import. The saved-token indicator confirms configuration, not a successful provider request.

You can run OAuth on your laptop instead and paste the resulting refresh token into the Jupyter-hosted app. Do not paste the login URL, callback URL or callback code into the refresh-token field. If the callback code expires or is rejected, start a fresh OAuth flow. Follow the current command output if Pixiv changes its login flow.

Reference: [gallery-dl Pixiv refresh-token configuration](https://gdl-org.github.io/docs/configuration.html#extractor-pixiv-refresh-token).

## Twitter / X on Jupyter (recommended: cookie upload)

The browser dropdown refers to a browser installed on the **backend machine**. Selecting Edge on a Jupyter server does not read your laptop's Edge session. Use cookie upload for that arrangement.

1. On your laptop, sign in to **x.com** with your own account.
2. Export cookies for that site in **Netscape cookies.txt format**. A JSON cookie export or a copied HTTP Cookie header will not work. Use a cookie-export tool you trust and limit the export to X/Twitter rather than unrelated sites.
3. In the **Jupyter-hosted app**, open **Settings > Twitter / X authentication**.
4. Click **Choose File**, select the exported file on your laptop, then click **Upload cookies.txt**. You do not need to upload it separately through Jupyter's file browser.
5. Wait for **Validated and saved** / **Cookie file active**. The app validates the file and requires nonempty `auth_token` and `ct0` cookies for x.com/twitter.com. Files must be no larger than 2 MiB.
6. Leave cookie-file mode selected. Do **not** click **Use browser mode** afterward; that switches away from the upload.
7. Try a small import from a full X artist/profile URL. File validation is not a live-login test.

If the app says `auth_token` or `ct0` is missing, confirm you are signed in, revisit x.com and export again. If a previously working session expires, export a new file and upload it again. Never post the cookie contents in an issue or chat.

### Optional export with gallery-dl on your laptop

If browser cookie extraction works on your machine, gallery-dl can export cookies without a browser extension. Run this from the Windows project directory, replacing the example URL with one real X post you can view:

```powershell
.\.venv\Scripts\gallery-dl.exe --cookies-from-browser edge/x.com --cookies-export library/twitter-export.cookies.txt --simulate --range 1 "https://x.com/ACCOUNT/status/POST_ID"
```

`--simulate` avoids downloading media, but the command still contacts X. After successful export, upload `library/twitter-export.cookies.txt` through the app as described above. Browser profiles can be specified using gallery-dl's documented syntax. If browser extraction fails due to a locked/encrypted Chromium cookie store, use a trusted Netscape-format export tool instead; closing the browser may address a lock but does not guarantee decryption support.

Reference: [gallery-dl cookie import/export options](https://gdl-org.github.io/docs/options.html#authentication-options).

### Local browser mode

For a backend running on the same laptop as the signed-in browser, choose the browser and optional profile, then click **Use browser mode**. Access depends on browser profile location and operating-system cookie protection. Cookie upload remains available as an alternative.

## ArtStation and Pawchive

When the runtime is ready, no additional login setup is exposed for the public sources supported by these adapters. Use an artist/profile URL for ArtStation and a full supported creator/post URL for Pawchive. These are artist/profile integrations, not general character/tag-search providers. A ready label does not guarantee every URL or restricted post is accessible.

## Where secrets live

Provider settings belong to the active backend library, normally `provider_settings.json`. Uploaded X cookies are stored under `.secrets/twitter.cookies.txt`. These paths are excluded from Git and credentials are not returned by the settings API. Private backups include them. Environment overrides can take precedence over saved settings; see [Development](DEVELOPMENT.md#configuration) if a saved change seems ineffective.

Use the authenticated Jupyter URL when entering credentials remotely. Keep token/cookie exports private, and delete temporary exported copies when you no longer need them. For failures, share a redacted error/request ID rather than tokens, cookie files or the library database.


## DeviantArt

DeviantArt supports **artist collections**. Create a collection with DeviantArt checked, or enable it under an existing collection's Sources. Use a username such as `artist-name`, a profile URL (`https://www.deviantart.com/artist-name`), a gallery URL, or a single artwork URL. If your booru artist tag differs from the DeviantArt username, enter the correct URL in the DeviantArt source's provider-specific search. This integration does not provide arbitrary character/tag searches. Bulk artist lists work in DeviantArt groups; entries should be DeviantArt usernames. Protected collections remain excluded from bulk and scheduled scraping.

Start with public access: no credentials are required for public API-accessible works. Availability depends on the site; private, mature, subscriber-only, or otherwise restricted works can require authorization and may remain unavailable. The app does not bypass access restrictions.

For OAuth, use a **terminal**, not a notebook cell. Run inside the app directory:

Windows:
```powershell
.\.venv\Scripts\gallery-dl.exe oauth:deviantart
```

Jupyter/Linux:
```bash
.venv/bin/gallery-dl oauth:deviantart
```

Follow gallery-dl's authorization instructions using your browser. Enter the final refresh-token in **Settings > DeviantArt OAuth** and save it. If you register a custom DeviantArt API application, configure gallery-dl with that client ID/secret before authorizing, and enter the same client ID and secret together with the resulting token in the app. Tokens must belong to the selected client. Refer to gallery-dl's [DeviantArt OAuth configuration](https://gdl-org.github.io/docs/configuration.html#extractor-deviantart-refresh-token) and [custom client instructions](https://gdl-org.github.io/docs/configuration.html#extractor-deviantart-client-id-client-secret). Never paste secrets into an issue, screenshot, or repository.

For a custom API client, keep a private JSON file outside the repository, for example `deviantart-auth.json`:

```json
{"extractor": {"deviantart": {"client-id": "YOUR_CLIENT_ID", "client-secret": "YOUR_CLIENT_SECRET"}}}
```

Run `.venv/bin/gallery-dl --config /path/to/deviantart-auth.json oauth:deviantart` on Linux, or `.\.venv\Scripts\gallery-dl.exe --config C:\path\to\deviantart-auth.json oauth:deviantart` on Windows. Copy the displayed authorization URL into your browser if the remote server cannot open one. Follow the prompt to complete authorization; use the final refresh-token, not the callback code. Keep or remove the temporary credential file privately after configuration.

Saving replaces all three DeviantArt credential fields. Empty fields clear saved values; to replace custom credentials, enter the full matching set. The Settings response shows only configured flags, never credential values. Credentials and the private OAuth refresh cache are stored on the backend you are connected to, so configure them again through the Jupyter-hosted app if you previously saved them only on your laptop. Saved means stored, not remotely validated: scraping reports authorization failures.

The integration requests original downloads where the site permits them and otherwise uses gallery-dl's available artwork content. Preview substitution is disabled. It retains source metadata and stable artwork IDs; tags are provenance-only, like the other profile providers, and do not automatically replace your ground-truth tags. Existing resize/WebP settings still apply. Gallery discovery uses gallery-dl's throttling with a minimum API wait setting of two seconds; increasing download workers does not remove site throttling. Oldest-first is unavailable; dates filter extracted results and do not imply server-side date search.

If a job fails: check the username/URL, confirm the work is accessible to your account, then verify your token and matching custom client credentials. Rate limits or site blocks may require waiting. Never treat a failed request as proof that the artist's gallery is empty.

## Upgrading existing libraries

Startup retires unsupported scrape configurations without deleting downloaded artwork or rewriting its original provenance. A group whose source has been retired displays **Choose a replacement provider**. Select its new group scrape provider, then explicitly enable that source and supply the correct profile query in member collections. Changing the group's provider alone never rewrites collection queries or enables scraping. Existing group directories and protected-folder lists remain intact.

DeviantArt can rotate OAuth tokens during use. The app retains gallery-dl's refresh cache privately under the library's `.secrets/deviantart/` directory so later pages and restarts can authenticate. Changing or clearing credentials selects a separate cache; older cache files are not reused, but remain private backup material. Do not publish the library or its backups.


### Original-download quota and published-image mode

If DeviantArt returns **Free download limit reached**, the original-download endpoint has refused the request. Increasing workers, clearing app data, or immediately retrying does not remove this site-side quota. OAuth identifies your account; it does not remove the weekly download allowance.

In **Settings > DeviantArt > Artwork download mode**, choose:

- **Request originals; stop on quota** (default): preserve the existing original-request behavior and stop with an explanation when the quota blocks it. Non-downloadable works can still use their published artwork content, as before.
- **Request originals; allow published fallback on quota**: retry the same discovery page once using published artwork only when this specific quota error occurs. The job and diagnostics log a warning, and imported source metadata records `published_after_original_quota`. Authentication failures and unrelated errors are not retried this way.
- **Artwork page / published images**: try the full-view variants advertised on the artwork page, then the API artwork URL, without calling the original-file download endpoint. These may be resized or recompressed.
- **Artwork page; allow thumbnail fallback**: use the same order, then try the largest advertised uncropped, unblurred page thumbnail. This applies only to DeviantArt static image posts, never video/archive previews. Thumbnail sizes vary; 1024 pixels is not guaranteed.

Changes save immediately and preserve OAuth settings. Restart the failed job after selecting a mode. Existing images are not replaced or upgraded when the mode changes. Published images remain subject to your processing and minimum-dimension filters, account permissions, and ordinary provider rate limits.

A successful listing does not guarantee media-file access. HTTP 401/403 on a listed artwork can mean account restrictions, an expired signed URL, or a site block. Save authorized OAuth credentials, verify the work is accessible to that account, and retry the job for fresh URLs. With a saved token, discovery uses authenticated API access instead of the public token. Published-image mode does not unlock restricted works.

Artwork-page downloads record `studio_media_selection` (`fullview` or `thumbnail`) and actual decoded dimensions in source metadata; diagnostics record the selected version. Artwork-page access uses uploaded DeviantArt cookies when available, independently of OAuth API discovery. Without cookies, pages are fetched as a logged-out visitor. There is no automatic upgrade of earlier imports. MuHut?s Under the moonlight was verified as a 1280 x 1600 full-view JPEG; this is not the 1600 x 2000 original.

### DeviantArt browser cookies (mature artwork-page access)

If a work is visible in your signed-in browser but fails in the app, use **Settings ? DeviantArt ? DeviantArt browser cookies**:

1. Sign in on deviantart.com and choose the appropriate Browsing Mode (Mature for eligible accounts viewing mature works). Verify the artwork is visible unblurred.
2. Export that site?s cookies as Netscape `cookies.txt` using a trusted browser tool.
3. Choose the file and click **Upload DeviantArt cookies**. It is saved privately on the backend serving this app, including when that backend runs on Jupyter.
4. Start a new sync using an artwork-page download mode. Existing running jobs may retain their previous settings.

Only DeviantArt cookies are retained. The upload validates format and presence of a non-expired login cookie; it does not prove the session still works. Re-export if the session expires or the site logs you out. **Remove DeviantArt cookies** removes the saved file without changing OAuth or download mode. Cookies cannot raise the original-download allowance or grant access your account lacks. Never commit or share the cookie file.
