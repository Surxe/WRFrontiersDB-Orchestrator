from typing import Literal
from pathlib import Path

"""
# Patterns
* **should_** - Stage gate flags (e.g., `should_export`). If every should_ option
  resolves to False, optionsconfig treats them all as True (no flags = full run).
* All stage paths are DERIVED from WRF_ROOT in src/repos.py; the only path knobs
  here are WRF_ROOT itself and REPOS_DIR. Secrets are overlaid from
  ~dev/.config/wrf-orchestrator/secrets.env and passed to sub-repos via env.
"""

OPTIONS_SCHEMA = {
    # ------------------------------------------------------------------ Logging
    "LOG_LEVEL": {
        "env": "LOG_LEVEL",
        "arg": "--log-level",
        "type": Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "default": "DEBUG",
        "section": "Logging",
        "help": "Logging level. Must be one of: TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL.",
    },
    "LOG_DIR": {
        "env": "LOG_DIR",
        "arg": "--log-dir",
        "type": Path,
        "default": Path("logs"),
        "section": "Logging",
        "help": "Directory for the orchestrator's own logs. Each run creates a "
                "timestamped subdir with one file per stage plus a run.log.",
    },

    # -------------------------------------------------------------------- Paths
    "WRF_ROOT": {
        "env": "WRF_ROOT",
        "arg": "--wrf-root",
        "type": Path,
        "default": Path("/srv/dev/wrf"),
        "section": "Paths",
        "help": "Shared root for all game data. Every stage directory "
                "(steam-download, mapper, exports, parsed, textures, prefix, "
                "proton) is derived from this. The only heavy path knob.",
        "example": Path("/srv/dev/wrf"),
    },
    "REPOS_DIR": {
        "env": "REPOS_DIR",
        "arg": "--repos-dir",
        "type": Path,
        "default": Path("/srv/dev/repos"),
        "section": "Paths",
        "help": "Parent directory holding the sibling repos "
                "(WRFrontiers-Exporter, WRFrontiersDB-Parser, WRFrontiersDB-Site, "
                "WRFrontiersDB-Data).",
        "example": Path("/srv/dev/repos"),
    },

    # ------------------------------------------------------------------ Secrets
    # Overlaid from ~dev/.config/wrf-orchestrator/secrets.env; never in .env.
    # Passed to sub-repos via the subprocess environment, not CLI args.
    "STEAM_USERNAME": {
        "env": "STEAM_USERNAME",
        "arg": "--steam-username",
        "type": str,
        "default": None,
        "section": "Secrets",
        "help": "Steam username for the Exporter's DepotDownloader step.",
        "example": "example_user",
    },
    "STEAM_PASSWORD": {
        "env": "STEAM_PASSWORD",
        "arg": "--steam-password",
        "type": str,
        "default": None,
        "sensitive": True,
        "section": "Secrets",
        "help": "Steam password for the Exporter's DepotDownloader step.",
        "example": "example_password",
    },
    "GH_DATA_REPO_PAT": {
        "env": "GH_DATA_REPO_PAT",
        "arg": "--gh-data-repo-pat",
        "type": str,
        "default": None,
        "sensitive": True,
        "section": "Secrets",
        "help": "PAT with push access to the data repo, for the Parser's push step.",
        "example": "github_pat_XXXXXXXXXXXXXXXX",
    },

    # -------------------------------------------------------------------- Patch
    "GAME_VERSION": {
        "env": "GAME_VERSION",
        "arg": "--game-version",
        "type": str,
        "default": None,
        "section": "Patch",
        "help": "Release date of the patch being processed, as yyyy-mm-dd. Names "
                "the .usmap, the data archive dir, and the Parser push. Confirmed "
                "against the SteamDB manifest date in preflight.",
        "example": "2026-08-22",
    },
    "MANIFEST_ID": {
        "env": "MANIFEST_ID",
        "arg": "--manifest-id",
        "type": str,
        "default": "latest",
        "section": "Patch",
        "help": "Steam manifest ID to download. 'latest' uses the newest manifest.",
        "links": {"SteamDB": "https://steamdb.info/app/1491000/depot/1491005/manifests/"},
    },
    "TARGET_BRANCH": {
        "env": "TARGET_BRANCH",
        "arg": "--target-branch",
        "type": str,
        "default": "dev",
        "section": "Patch",
        "help": "Branch in the data repo the Parser pushes to.",
    },
    "ASSUME_MANIFEST_CONFIRMED": {
        "env": "ASSUME_MANIFEST_CONFIRMED",
        "arg": "--assume-manifest-confirmed",
        "type": bool,
        "default": False,
        "section": "Patch",
        "help": "Skip the interactive manifest-date confirmation gate. Requires "
                "GAME_VERSION to be set explicitly. For non-interactive runs only.",
    },

    # ------------------------------------------------------------------- Stages
    "SHOULD_EXPORT": {
        "env": "SHOULD_EXPORT",
        "arg": "--should-export",
        "type": bool,
        "default": False,
        "section": "Stages",
        "help": "Run the Exporter (Steam download, mapper, BatchExport).",
    },
    "SHOULD_PARSE": {
        "env": "SHOULD_PARSE",
        "arg": "--should-parse",
        "type": bool,
        "default": False,
        "section": "Stages",
        "help": "Run the Parser over the exported JSON.",
    },
    "SHOULD_PUSH_DATA": {
        "env": "SHOULD_PUSH_DATA",
        "arg": "--should-push-data",
        "type": bool,
        "default": False,
        "section": "Stages",
        "help": "Push the parsed data to the data repo (implies parse output exists).",
    },
    "SHOULD_BUILD_SITE": {
        "env": "SHOULD_BUILD_SITE",
        "arg": "--should-build-site",
        "type": bool,
        "default": False,
        "section": "Stages",
        "help": "Build the Astro site (npm run build) against the updated data repo.",
    },

    # -------------------------------------------------- Exporter sub-steps (fwd)
    # Forwarded to the Exporter when SHOULD_EXPORT is on, so a patch-day run can
    # skip pieces (e.g. re-export without re-downloading 60 GB from Steam).
    "SHOULD_DOWNLOAD_DEPENDENCIES": {
        "env": "SHOULD_DOWNLOAD_DEPENDENCIES",
        "arg": "--should-download-dependencies",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: download/update its tool dependencies.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "FORCE_DOWNLOAD_DEPENDENCIES": {
        "env": "FORCE_DOWNLOAD_DEPENDENCIES",
        "arg": "--force-download-dependencies",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: re-download dependencies even if "
                "already present.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "SHOULD_DOWNLOAD_STEAM_GAME": {
        "env": "SHOULD_DOWNLOAD_STEAM_GAME",
        "arg": "--should-download-steam-game",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: download/update the game via DepotDownloader.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "FORCE_STEAM_DOWNLOAD": {
        "env": "FORCE_STEAM_DOWNLOAD",
        "arg": "--force-steam-download",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: re-download/update the game even if "
                "already present.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "SHOULD_GET_MAPPER": {
        "env": "SHOULD_GET_MAPPER",
        "arg": "--should-get-mapper",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: generate the .usmap (runs as dev under "
                "gamescope headless; see README).",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "FORCE_GET_MAPPER": {
        "env": "FORCE_GET_MAPPER",
        "arg": "--force-get-mapper",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: re-generate the .usmap even if it "
                "already exists.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "SHOULD_BATCH_EXPORT": {
        "env": "SHOULD_BATCH_EXPORT",
        "arg": "--should-batch-export",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: run BatchExport to produce JSON.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "FORCE_EXPORT": {
        "env": "FORCE_EXPORT",
        "arg": "--force-export",
        "type": bool,
        "default": False,
        "section": "Exporter",
        "help": "Forwarded to the Exporter: re-run BatchExport even if the output "
                "directory is not empty.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "SHOULD_EXPORT_TEXTURES": {
        "env": "SHOULD_EXPORT_TEXTURES",
        "arg": "--should-export-textures",
        "type": bool,
        "default": True,
        "section": "Exporter",
        "help": "Forwarded to the Exporter/Parser: export and push textures.",
        "depends_on": ["SHOULD_EXPORT"],
    },
    "HEADLESS": {
        "env": "HEADLESS",
        "arg": "--headless",
        "type": bool,
        "default": True,
        "section": "Exporter",
        "help": "Forwarded to the Exporter mapper step: launch under gamescope's "
                "headless backend. Default true (unattended dev runs).",
        "depends_on": ["SHOULD_EXPORT"],
    },
}
