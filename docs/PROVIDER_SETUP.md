# Provider setup: local and Jupyter

Complete these steps in the Settings page of the instance you want to use. If the app runs on Jupyter, credentials must be saved in that server-hosted app. Cloning GitHub does not copy your laptop credentials. You normally do not need to restart the backend after saving credentials.

## Before starting

1. Open **Settings** and confirm **Active server and storage** shows the intended library.
2. Check that the gallery-dl runtime reports ready. If not, run the app's setup using its virtual environment and restart.
3. Choose the provider below. A configured/ready indicator is not proof of successful live access; test a small import afterward and inspect Logs if it fails.

| Provider | Setup in this app |
| --- | --- |
| Danbooru, e621, yande.re | No additional credential fields currently exposed; availability/search restrictions depend on the site. |
| Gelbooru | Numeric user ID and API key |
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
