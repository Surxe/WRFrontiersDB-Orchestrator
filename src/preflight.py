"""Preflight: the manifest-date gate plus path/venv/secret validation.

Nothing heavy runs until preflight passes, so a bad path or a missing secret
fails in seconds rather than after a multi-hour Steam pull.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from config import CANONICAL_SECRETS_PATH, missing_secrets
from repos import Repos

MANIFEST_URL = "https://steamdb.info/depot/1491005/manifests/"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PreflightError(Exception):
    """Raised when validation fails; message is user-facing."""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def confirm_game_version(options, *, interactive: bool) -> str:
    """Resolve and confirm GAME_VERSION against the SteamDB manifest date.

    Timezones can make a patch look like it dropped on a different calendar day,
    so we make the human eyeball the manifest's release date before committing to
    a version string (which names the .usmap, the archive dir, and the push).
    """
    given = (options.game_version or "").strip()
    tz = datetime.now().astimezone().tzname() or "local"
    today = _today()

    if not interactive or options.assume_manifest_confirmed:
        if not given:
            raise PreflightError(
                "Non-interactive run: --game-version is required together with "
                "--assume-manifest-confirmed (cannot confirm the manifest date "
                "without a human)."
            )
        if not _DATE_RE.match(given):
            raise PreflightError(f"GAME_VERSION must be yyyy-mm-dd, got: {given!r}")
        print(f"[preflight] Manifest confirmation skipped; using GAME_VERSION={given}")
        return given

    print()
    print("Manifest date check (timezones can shift the calendar day):")
    print(f"  Open:  {MANIFEST_URL}")
    print(f"  Today: {today}  ({tz})")
    print("  Read the LATEST manifest's 'Last Updated' date and compare.")
    if given:
        prompt = (
            f"  GAME_VERSION is set to {given}. Does the manifest date match it? "
            f"[y = yes / N = no / or type the correct yyyy-mm-dd]: "
        )
    else:
        prompt = (
            f"  Does the latest manifest's date match today ({today})? "
            f"[y = yes, use {today} / N = no / or type the correct yyyy-mm-dd]: "
        )

    answer = input(prompt).strip()
    if _DATE_RE.match(answer):
        chosen = answer
    elif answer.lower() in ("y", "yes"):
        chosen = given or today
    else:
        # explicit no (or anything else) -> ask for the real date
        chosen = input("  Enter the manifest release date (yyyy-mm-dd): ").strip()

    if not _DATE_RE.match(chosen):
        raise PreflightError(f"Not a valid yyyy-mm-dd date: {chosen!r}")
    print(f"[preflight] Using GAME_VERSION={chosen}")
    return chosen


def validate(options, repos: Repos, *, game_version: str) -> None:
    """Validate paths, sub-repo venvs, and the secrets each active stage needs."""
    errors: list[str] = []

    if not repos.wrf_root.is_dir():
        errors.append(f"WRF_ROOT does not exist: {repos.wrf_root}")
    if not repos.repos_dir.is_dir():
        errors.append(f"REPOS_DIR does not exist: {repos.repos_dir}")

    if options.should_export:
        py = repos.venv_python(repos.exporter_dir)
        if not py.exists():
            errors.append(f"Exporter venv python missing: {py}")
        if not repos.proton_path.is_dir() and options.should_get_mapper:
            errors.append(f"Proton build missing (mapper needs it): {repos.proton_path}")
        need = [k for k in ("STEAM_USERNAME", "STEAM_PASSWORD")]
        if options.should_download_steam_game:
            for k in missing_secrets(tuple(need)):
                errors.append(f"Secret {k} required for the Steam download step "
                              f"(inject it from {CANONICAL_SECRETS_PATH})")

    if options.should_parse or options.should_push_data:
        py = repos.venv_python(repos.parser_dir)
        if not py.exists():
            errors.append(f"Parser venv python missing: {py}")
        # parse needs exports present unless export runs first in this same run
        if options.should_parse and not options.should_export:
            if not repos.exports_dir.is_dir():
                errors.append(f"EXPORT dir missing (run export first?): {repos.exports_dir}")

    if options.should_push_data:
        for k in missing_secrets(("GH_DATA_REPO_PAT",)):
            errors.append(f"Secret {k} required for the data push step "
                          f"(inject it from {CANONICAL_SECRETS_PATH})")
        if not repos.data_dir.is_dir():
            errors.append(f"Data repo checkout missing: {repos.data_dir}")

    if options.should_build_site:
        if not (repos.site_dir / "package.json").is_file():
            errors.append(f"Site repo missing package.json: {repos.site_dir}")

    # BatchExport needs a mapper; if we won't generate one, it must already exist.
    if options.should_export and options.should_batch_export and not options.should_get_mapper:
        mf = repos.mapper_file(game_version)
        if not mf.exists():
            errors.append(
                f"BatchExport requested without generating the mapper, but no "
                f"mapper file exists at {mf}. Enable --should-get-mapper or place "
                f"the .usmap there."
            )

    if errors:
        raise PreflightError(
            "Preflight failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
    print("[preflight] OK")
