# WRFrontiersDB-Orchestrator

Drives the War Robots Frontiers **patch-day data pipeline** end to end, chaining
the standalone tool repos so a new patch becomes one command instead of a manual
run of each stage. It replaces the old, never-checked-in `WRFrontiersDB-Scripts`
glue, with every PC-specific path derived from a single root and every secret
kept out of the repo (and out of `dev`'s reach).

## Pipeline

```
preflight ──▶ EXPORT ──▶ PARSE ──▶ (PUSH) ──▶ RELEASES ──▶ SITE
             (Exporter)  (Parser)             (roster diff) (Astro build)
```

| Stage | Repo | What it does |
| --- | --- | --- |
| EXPORT | WRFrontiers-Exporter | Steam download → mapper (`.usmap`) → BatchExport (JSON) |
| PARSE | WRFrontiersDB-Parser | Parse the exported JSON → parsed data + textures |
| PUSH | WRFrontiersDB-Parser | Push parsed data to WRFrontiersDB-Data (`current/` swap + archive) |
| RELEASES | (this repo) | Diff the pushed `VirtualBot.json` roster vs `curated/robot_release_dates.json` → record newly-released robots (commit + push) |
| SITE | WRFrontiersDB-Site | `npm run build` against the updated data repo |

### RELEASES — newly-released robot detection

A robot is a `VirtualBot` in the published data iff its modules are used by a
factory preset, and the studio only ships an obtainable robot with one — so a new
id in `current/Objects/VirtualBot.json` **is** the release signal (no
`ProductionStatus` check needed; the Parser gates on the preset, which agrees 1:1
with `Ready` core modules).

The **store is the dedup source**: the data repo's
`curated/robot_release_dates.json` already lists every robot with
`virtual_bot_ref = OBJID_VirtualBot::<slug>`. A roster id whose ref is already
there is already recorded, so detection is a pure file comparison — **no git diff
against the previous patch and no separate state file**. For each unrecorded
roster id the stage (`src/releases.py`) either:

- **backfills** the ref onto a pre-recorded entry whose `virtual_bot_ref` is null
  (a robot the news-scraper logged before it hit the roster — e.g. Angler),
  filling only still-null fields so hand-curated data is never overwritten; or
- **appends** a new entry (Mechs -> `robots[]`, Titans -> `titans[]`).

An auto-added entry carries only what the pipeline can source: `release_date` (the
in-house version id), `manifest_id` (`data/steam-download/manifest.txt`), and
`patch_released_at_utc` (the probe's state-file `timeupdated`, used only when its
`last_gid` matches the built manifest, else null — offline only, no live Steam
lookup). `release_context` and `source_article_ids` are left for the news-scraper /
a human. A patch with no new robots is a no-op: nothing is written, committed, or
errored.

- **Publish:** the edit is committed + pushed to the data repo. The Parser's push
  reclones a fresh checkout each run, so an uncommitted local edit would be wiped
  — this push is the only git operation, and it is output, not comparison. It runs
  only when `--should-push-data` is on and a PAT is present.
- **Standalone:** `.venv/bin/python src/releases.py` (`--no-write` to detect only;
  exit `20` = new robot, `0` = none, `1` = error).
- **Rename guard:** the id is `slugify(<localized name>)`, so a rename is rare; if
  a recorded ref's slug leaves the roster the same run a new id appears, the new
  bot is flagged `suspected_rename` for a human to confirm (WRF does not retire
  robots). Relic variants are distinct ids and are recorded as ordinary new robots.

The SITE build resolves its styling from **WRFrontiersDB-Design**, the shared
design system (tokens + self-hosted brand font) that both front-ends —
WRFrontiersDB-Site and the WRFrontiers-Discount-Visualizer — vendor as a git
submodule to stay visually in sync. It is not a pipeline stage the orchestrator
runs; each consuming site pulls it in at build time.

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

On this workstation the pipeline is normally launched by ethan via the
`wrf-orchestrator` bin (see [Launch on the workstation](#launch-on-the-workstation)),
not by calling `run.py` directly. The examples below show the underlying
`run.py` invocation; substitute `bin/run-pipeline` for `python src/run.py` to get
the node/npm environment set up automatically (see below).

```bash
# Full patch day (all stages; preflight confirms the version against the manifest)
python src/run.py --patch-day --game-version 2026-08-22

# Force a patch day: re-download and re-export even where output already exists
python src/run.py --force-patch-day --game-version 2026-08-22

# Re-parse + rebuild the site from an already-exported dump (no Steam pull)
python src/run.py --should-parse true --should-build-site true \
                  --game-version 2026-08-22

# Non-interactive (e.g. a scheduled run): version + gate skip both required
python src/run.py --patch-day --game-version 2026-08-22 --assume-manifest-confirmed true
```

Running with **no** `SHOULD_` flags runs the whole pipeline (all stages).
Value priority is **argument > .env parameter > default**.

### Shortcuts

These preset flags fill in the individual `--should-*` / `--force-*` gates so a
common run is one flag. They only set gates you leave unset, so an explicit
`--should-*` / `--force-*` on the same command still wins.

- **`--patch-day`** — the whole pipeline: every stage (export, parse, push, build
  site) and every Exporter sub-step. Equivalent to passing no `SHOULD_` flags,
  but named and explicit.
- **`--force-patch-day`** — `--patch-day` plus every `--force-*` flag, so each
  stage re-does work whose output already exists (re-download dependencies,
  re-download the game, re-generate the mapper, re-run BatchExport).

### Launch on the workstation

The pipeline is split into a privileged launcher and a dev-side entry:

- **`wrf-orchestrator`** (in `my-system`, deployed to `~ethan/.local/bin`) is the
  ethan-side wrapper. It reads the ethan-owned secrets and hops to `dev` via
  `sudo -u dev`, whose fresh initgroups is what grants the mapper its
  `render`/`video` groups. It does nothing else.
- **`bin/run-pipeline`** (this repo) is the dev-side entry the wrapper execs. It
  owns the run *environment* — it sources dev's **nvm** so `node`/`npm` are on
  `PATH` — then `cd`s here and execs `run.py` with the passed-through args.

The nvm step matters because the launch is a non-interactive shell: dev's
`~/.bashrc` (which sources nvm) never runs, so without this the SITE stage's
`npm run build` dies with `FileNotFoundError: 'npm'`. Running `bin/run-pipeline`
directly as dev works too (re-sourcing nvm is a no-op).

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

* **GAME_VERSION** - Version id of the patch being processed, as yyyy-mm-dd[-N]. Names the .usmap, the data archive dir, and the Parser push. By default the probe derives it from the manifest's unix timestamp (UTC day); a 2nd+ patch on the same day gets a -N suffix (-1, -2, ...). Pass explicitly to override.
  - Example: `"2026-08-22"`
  - Default: None
  - Command line: `--game-version`

* **MANIFEST_ID** - Steam manifest ID to download. 'latest' uses the newest manifest.
  - Default: `"latest"`
  - Command line: `--manifest-id`
  - [SteamDB](https://steamdb.info/app/1491000/depot/1491005/manifests/)

* **TARGET_BRANCH** - Branch in the data repo the Parser pushes to.
  - Default: `"dev"`
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

* **SHOULD_DETECT_RELEASES** - Diff the pushed data repo's VirtualBot roster against curated/robot_release_dates.json to detect newly-released robots, record them there (version id + manifest id + UTC patch time), and commit/push that file. Reads the data repo, so it wants parse/push to have run first.
  - Default: `"false"`
  - Command line: `--should-detect-releases`

* **SHOULD_BUILD_SITE** - Build the Astro site (npm run build:slugs + npm run build) locally against the updated data repo. A pre-flight that catches build breaks before SHOULD_DEPLOY_SITE spends CI minutes; does not deploy.
  - Default: `"false"`
  - Command line: `--should-build-site`

* **SHOULD_DEPLOY_SITE** - Deploy the site: dispatch WRFrontiersDB-Site's GitHub Pages workflow (pages.yaml) on its main branch via `gh workflow run`, so CI rebuilds and publishes against the freshly-pushed data. Fires immediately when on (no dry-run gate); needs `gh` authed with Actions-dispatch rights on Surxe/WRFrontiersDB-Site. Runs after SITE, so a local build break stops the pipeline before this dispatches.
  - Default: `"false"`
  - Command line: `--should-deploy-site`


#### Exporter

* **SHOULD_DOWNLOAD_DEPENDENCIES** - Forwarded to the Exporter: download/update its tool dependencies.
  - Default: `"false"`
  - Command line: `--should-download-dependencies`
  - Depends on: `SHOULD_EXPORT`

* **FORCE_DOWNLOAD_DEPENDENCIES** - Forwarded to the Exporter: re-download dependencies even if already present.
  - Default: `"false"`
  - Command line: `--force-download-dependencies`
  - Depends on: `SHOULD_EXPORT`

* **BATCH_EXPORT_RELEASE** - Forwarded to the Exporter: CUE4P-BatchExport release tag to install. 'latest' uses the newest stable release; set a tag (e.g. 'v1.6.2-test.1') to pin a version. Roll back by setting this to 'latest' again with SHOULD_DOWNLOAD_DEPENDENCIES on.
  - Default: `"latest"`
  - Command line: `--batch-export-release`
  - Depends on: `SHOULD_EXPORT`
  - [Releases](https://github.com/Surxe/CUE4P-BatchExport/releases)

* **SHOULD_DOWNLOAD_STEAM_GAME** - Forwarded to the Exporter: download/update the game via DepotDownloader.
  - Default: `"false"`
  - Command line: `--should-download-steam-game`
  - Depends on: `SHOULD_EXPORT`

* **FORCE_STEAM_DOWNLOAD** - Forwarded to the Exporter: re-download/update the game even if already present.
  - Default: `"false"`
  - Command line: `--force-steam-download`
  - Depends on: `SHOULD_EXPORT`

* **SHOULD_GET_MAPPER** - Forwarded to the Exporter: generate the .usmap (runs as dev under gamescope headless; see README).
  - Default: `"false"`
  - Command line: `--should-get-mapper`
  - Depends on: `SHOULD_EXPORT`

* **FORCE_GET_MAPPER** - Forwarded to the Exporter: re-generate the .usmap even if it already exists.
  - Default: `"false"`
  - Command line: `--force-get-mapper`
  - Depends on: `SHOULD_EXPORT`

* **SHOULD_BATCH_EXPORT** - Forwarded to the Exporter: run BatchExport to produce JSON.
  - Default: `"false"`
  - Command line: `--should-batch-export`
  - Depends on: `SHOULD_EXPORT`

* **FORCE_EXPORT** - Forwarded to the Exporter: re-run BatchExport even if the output directory is not empty.
  - Default: `"false"`
  - Command line: `--force-export`
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
