# WRFrontiersDB-Orchestrator

Drives the War Robots Frontiers **patch-day data pipeline** end to end, chaining
the standalone tool repos so a new patch becomes one command instead of a manual
run of each stage. It replaces the old, never-checked-in `WRFrontiersDB-Scripts`
glue, with every PC-specific path derived from a single root and every secret
kept out of the repo (and out of `dev`'s reach).

## Pipeline

```
preflight ──▶ EXPORT ──▶ PARSE ──▶ (PUSH) ──▶ SITE
             (Exporter)  (Parser)             (Astro build)
```

| Stage | Repo | What it does |
| --- | --- | --- |
| EXPORT | WRFrontiers-Exporter | Steam download → mapper (`.usmap`) → BatchExport (JSON) |
| PARSE | WRFrontiersDB-Parser | Parse the exported JSON → parsed data + textures |
| PUSH | WRFrontiersDB-Parser | Push parsed data to WRFrontiersDB-Data (`current/` swap + archive) |
| SITE | WRFrontiersDB-Site | `npm run build` against the updated data repo |

Out of scope for now: the discount visualizer and the news-research snapshot
(they have their own cadence; a tighter pipeline there comes later).

## Design

- **One root, everything derived.** `WRF_ROOT` (default `/srv/dev/wrf`) and
  `REPOS_DIR` are the only machine-specific knobs. Every stage directory is
  derived in `src/repos.py`, so the Exporter's output and the Parser's input can
  never drift apart (they were literally different paths before this repo).
- **Secrets never touch the repo — or `dev`.** See [Secrets](#secrets).
- **Streamed, per-stage logs.** Each stage's child process is streamed live to
  the console *and* teed to `LOG_DIR/<run-timestamp>/<NN-stage>.log`, with
  children run under `PYTHONUNBUFFERED=1` so nothing is swallowed by block
  buffering (the old "it pauses, only the .log has output" problem).
- **Manifest-date gate.** Before committing to a version string, preflight makes
  you eyeball the SteamDB manifest's release date against today — timezones can
  otherwise mislabel which calendar day a patch dropped.

## The mapper runs as `dev`

The Exporter's mapper step launches WRF under Proton via `gamescope --backend
headless`, which needs GPU access. `dev` was granted the `render` + `video`
groups and a persistent `XDG_RUNTIME_DIR` (`/run/user/1001`) so the whole
pipeline — mapper included — runs unattended as `dev`. See
`my-system/users-and-permissions/groups.md`. The export stage injects
`XDG_RUNTIME_DIR` and prepends `/usr/games` (where gamescope lives) to `PATH`
automatically.

## Setup

```bash
cd /srv/dev/repos/WRFrontiersDB-Orchestrator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Non-secret config (paths, toggles). Safe to keep in the repo dir; gitignored.
cp .env.example .env      # edit if your paths differ from the defaults
```

Each sibling repo must already have its own `.venv` (the orchestrator calls each
repo's `.venv/bin/python src/run.py`).

## Secrets

Three secrets are needed: `STEAM_USERNAME`, `STEAM_PASSWORD`, `GH_DATA_REPO_PAT`.

They are **not** stored in this repo, and **not** anywhere `dev` can read them —
`dev` is the repo-writable, AI-touched account. They live in ethan's space,
mode 600, exactly like `~ethan/.config/steam-price-tracker/smtp.env`:

```
~ethan/.config/wrf-orchestrator/secrets.env    (chmod 600, ethan-owned)
```

Create it once, **as ethan**:

```bash
install -d -m 700 ~/.config/wrf-orchestrator
cp /srv/dev/repos/WRFrontiersDB-Orchestrator/secrets.env.example \
   ~/.config/wrf-orchestrator/secrets.env
chmod 600 ~/.config/wrf-orchestrator/secrets.env
# then edit in the three values by hand
```

Because the pipeline runs as `dev`, it cannot read that file. An **ethan-owned
launcher** (a follow-up, to live in `my-system` and be review-gated) sources it
and injects the values into the `dev` run's environment:

```bash
set -a; . ~/.config/wrf-orchestrator/secrets.env; set +a
sudo -u dev --preserve-env=STEAM_USERNAME,STEAM_PASSWORD,GH_DATA_REPO_PAT \
  /srv/dev/repos/WRFrontiersDB-Orchestrator/.venv/bin/python src/run.py \
  --game-version 2026-08-22
```

So `dev` only ever sees the secrets transiently in a running process, never at
rest. Preflight fails early, naming the missing secret, if a stage that needs one
is enabled without it.

## Usage

```bash
# Full patch day (all stages; preflight confirms the version against the manifest)
python src/run.py --game-version 2026-08-22

# Re-parse + rebuild the site from an already-exported dump (no Steam pull)
python src/run.py --should-parse true --should-build-site true \
                  --game-version 2026-08-22

# Non-interactive (e.g. a scheduled run): version + gate skip both required
python src/run.py --game-version 2026-08-22 --assume-manifest-confirmed true
```

Running with **no** `SHOULD_` flags runs the whole pipeline (all stages).
Value priority is **argument > .env parameter > default**.

## Options

<!-- BEGIN_GENERATED_OPTIONS -->
#### Logging

* **LOG_LEVEL** - Logging level. Must be one of: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL.
  - Default: `"DEBUG"`
  - Command line: `--log-level`

* **LOG_DIR** - Directory for the orchestrator's own logs. Each run creates a timestamped subdir with one file per stage plus a run.log.
  - Default: `"logs"`
  - Command line: `--log-dir`


#### Paths

* **WRF_ROOT** - Shared root for all game data. Every stage directory (steam-download, mapper, exports, parsed, textures, prefix, proton) is derived from this. The only heavy path knob.
  - Example: `"/srv/dev/wrf"`
  - Default: `"/srv/dev/wrf"`
  - Command line: `--wrf-root`

* **REPOS_DIR** - Parent directory holding the sibling repos (WRFrontiers-Exporter, WRFrontiersDB-Parser, WRFrontiersDB-Site, WRFrontiersDB-Data).
  - Example: `"/srv/dev/repos"`
  - Default: `"/srv/dev/repos"`
  - Command line: `--repos-dir`


#### Secrets

* **STEAM_USERNAME** - Steam username for the Exporter's DepotDownloader step.
  - Example: `"example_user"`
  - Default: None
  - Command line: `--steam-username`

* **STEAM_PASSWORD** - Steam password for the Exporter's DepotDownloader step.
  - Example: `"example_password"`
  - Default: None
  - Command line: `--steam-password`

* **GH_DATA_REPO_PAT** - PAT with push access to the data repo, for the Parser's push step.
  - Example: `"github_pat_XXXXXXXXXXXXXXXX"`
  - Default: None
  - Command line: `--gh-data-repo-pat`


#### Patch

* **GAME_VERSION** - Release date of the patch being processed, as yyyy-mm-dd. Names the .usmap, the data archive dir, and the Parser push. Confirmed against the SteamDB manifest date in preflight.
  - Example: `"2026-08-22"`
  - Default: None
  - Command line: `--game-version`

* **MANIFEST_ID** - Steam manifest ID to download. 'latest' uses the newest manifest.
  - Default: `"latest"`
  - Command line: `--manifest-id`
  - [SteamDB](https://steamdb.info/app/1491000/depot/1491005/manifests/)

* **TARGET_BRANCH** - Branch in the data repo the Parser pushes to.
  - Default: `"testing-grounds"`
  - Command line: `--target-branch`

* **ASSUME_MANIFEST_CONFIRMED** - Skip the interactive manifest-date confirmation gate. Requires GAME_VERSION to be set explicitly. For non-interactive runs only.
  - Default: `"false"`
  - Command line: `--assume-manifest-confirmed`


#### Stages

* **SHOULD_EXPORT** - Run the Exporter (Steam download, mapper, BatchExport).
  - Default: `"false"`
  - Command line: `--should-export`

* **SHOULD_PARSE** - Run the Parser over the exported JSON.
  - Default: `"false"`
  - Command line: `--should-parse`

* **SHOULD_PUSH_DATA** - Push the parsed data to the data repo (implies parse output exists).
  - Default: `"false"`
  - Command line: `--should-push-data`

* **SHOULD_BUILD_SITE** - Build the Astro site (npm run build) against the updated data repo.
  - Default: `"false"`
  - Command line: `--should-build-site`


#### Exporter

* **SHOULD_DOWNLOAD_DEPENDENCIES** - Forwarded to the Exporter: download/update its tool dependencies.
  - Default: `"false"`
  - Command line: `--should-download-dependencies`
  - Depends on: `SHOULD_EXPORT`

* **SHOULD_DOWNLOAD_STEAM_GAME** - Forwarded to the Exporter: download/update the game via DepotDownloader.
  - Default: `"false"`
  - Command line: `--should-download-steam-game`
  - Depends on: `SHOULD_EXPORT`

* **SHOULD_GET_MAPPER** - Forwarded to the Exporter: generate the .usmap (runs as dev under gamescope headless; see README).
  - Default: `"false"`
  - Command line: `--should-get-mapper`
  - Depends on: `SHOULD_EXPORT`

* **SHOULD_BATCH_EXPORT** - Forwarded to the Exporter: run BatchExport to produce JSON.
  - Default: `"false"`
  - Command line: `--should-batch-export`
  - Depends on: `SHOULD_EXPORT`

* **SHOULD_EXPORT_TEXTURES** - Forwarded to the Exporter/Parser: export and push textures.
  - Default: `"true"`
  - Command line: `--should-export-textures`
  - Depends on: `SHOULD_EXPORT`

* **HEADLESS** - Forwarded to the Exporter mapper step: launch under gamescope's headless backend. Default true (unattended dev runs).
  - Default: `"true"`
  - Command line: `--headless`
  - Depends on: `SHOULD_EXPORT`


<!-- END_GENERATED_OPTIONS -->

## Contributing

- After editing `options_schema.py`, run `python build/docs.py` to regenerate
  `.env.example` and this README's options block.
- Follow `STANDARDS.md`.

## Disclaimer

For educational and research purposes. Comply with the War Robots Frontiers and
Steam terms of service.
