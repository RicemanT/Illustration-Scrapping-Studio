# Publishing the repository

The repository root is the directory containing `studio.py` and `IMPLEMENTATION_GUIDE.md`, not its parent workspace. The README is intentionally minimal so the owner can write the GitHub front page later.

## Included

Source, tests, synthetic benchmark code, the output-free Jupyter notebook, dependency lockfiles, documentation, launchers, MIT license and CI workflow. The original specification is marked historical. The implementation guide remains at the root.

## Excluded

The complete `library/` (including preserved credentials/settings), `.venv`, `node_modules`, built frontend, backups, logs, local environment files, secrets and optional release archives. Ignore rules do not remove already tracked files. Do not use `git add -f` for these paths. Custom libraries/archives should be outside the repository.

Before committing, run:

```bash
python tools/check_repository.py
git status --short
git diff --cached --stat
```

The checker reports paths/rule names without printing matching secret values. It checks common accidental artifacts and credential patterns; it is not a guarantee that all possible secrets are detected. Inspect the staged file list yourself as well.

Create an empty GitHub repository under your own account. Once you choose its URL:

```bash
git add .
git diff --cached --stat
git commit -m "Initial public version"
git remote add origin https://github.com/RicemanT/Illustration-Scrapper-Studio.git
git push -u origin main
```

No remote is preconfigured and no publishing command is run by setup. GitHub Actions will run the configured checks after a push; its results are separate from local verification.

## Jupyter installation from GitHub

In the Jupyter terminal, clone your actual repository URL, open `Jupyter Studio.ipynb`, and set `APP` to the cloned directory. See [JupyterHub](JUPYTER.md). GitHub is convenient, not mandatory: a source upload works too. Since built frontend files are ignored, a host without Node needs a separate upload of `frontend/dist` after you build it locally.

Provider credentials intentionally do not come from GitHub. Enter them again in the server UI, or privately migrate a backup using the documented restore process. Never publish your configured laptop library to transfer credentials.

## Optional release downloads

A ZIP is optional and separate from the source repository. Build the frontend, then run `python tools/build_release.py releases/studio-VERSION.zip`. The builder includes compiled UI and runtime code with documentation/license, excludes the personal library, writes a SHA-256 sidecar and refuses to overwrite existing output. Read [Installation](INSTALLATION.md) for requirements. A ZIP is not a standalone executable; Python dependencies still need installation.
