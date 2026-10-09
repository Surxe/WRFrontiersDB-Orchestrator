"""Git and import access to the WRFrontiersDB-Data checkout.

The data repo owns the code that builds its `index/` (`tools/wrfdb_data`, standard
library only); the orchestrator imports it from the checkout the Parser just pushed
and publishes what it writes. Publishing is a commit + push, because the Parser's
push reclones a fresh checkout each run: an uncommitted local edit would be
discarded before it ever reached the remote.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

DATA_REPO_SLUG = "Surxe/WRFrontiersDB-Data"
TOOLS_REL = Path("tools")

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Orchestrator",
    "GIT_AUTHOR_EMAIL": "orchestrator@example.com",
    "GIT_COMMITTER_NAME": "Orchestrator",
    "GIT_COMMITTER_EMAIL": "orchestrator@example.com",
}
"""The index commits' author, set per git process like the PAT: the data clone is
shared (people and the Parser commit there too), so nothing goes in its .git/config."""

LEGACY_LOCAL_CONFIG = ("user.email", "user.name")
"""Written to the clone's .git/config by older versions; removed before publishing."""


class DataRepoGitError(RuntimeError):
    """A git operation on the data repo failed (the PAT is redacted from the message)."""


def import_tools(data_dir: Path):
    """Import `wrfdb_data` from the data checkout's `tools/` and return the package.

    The checkout is recloned each run, so the path is put first on sys.path and
    the import happens only when a stage needs it.
    """
    tools_dir = str(Path(data_dir) / TOOLS_REL)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import wrfdb_data.abbreviations  # noqa: F401
    import wrfdb_data.build_codes  # noqa: F401
    import wrfdb_data.nicknames  # noqa: F401
    import wrfdb_data.paths  # noqa: F401
    import wrfdb_data.releases  # noqa: F401
    import wrfdb_data.robot_parts  # noqa: F401
    import wrfdb_data.slug_map  # noqa: F401
    import wrfdb_data
    return wrfdb_data


def _basic_auth_value(pat: str) -> str:
    return base64.b64encode(f"x-access-token:{pat}".encode()).decode()


def _git_auth_env(pat: str) -> dict[str, str]:
    """Authenticate one git process to GitHub with `pat`, via environment config.

    GIT_CONFIG_COUNT/KEY/VALUE (git 2.31+) sets an http.extraheader for this
    process only, the way actions/checkout does. A PAT embedded in the remote URL
    would be written to the data clone's .git/config and stay on disk.
    """
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {_basic_auth_value(pat)}",
    }


def _git(args: list[str], cwd: Path, pat: str | None = None, *,
         ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    env.update(GIT_IDENTITY)
    if pat:
        env.update(_git_auth_env(pat))
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
    )
    if proc.returncode not in ok_codes:
        out = (proc.stdout or "") + (proc.stderr or "")
        if pat:
            out = out.replace(pat, "********").replace(_basic_auth_value(pat), "********")
        raise DataRepoGitError(f"git {args[0]} failed: {out.strip()}")
    return proc


def publish(data_dir: Path, paths: list[Path], *, pat: str, branch: str, message: str) -> bool:
    """Commit `paths` (relative to the checkout) and push them on `branch`.

    Returns False when git sees no change, so nothing was committed.
    """
    data_dir = Path(data_dir)
    # Exit 5: the key wasn't set.
    for key in LEGACY_LOCAL_CONFIG:
        _git(["config", "--local", "--unset-all", key], data_dir, ok_codes=(0, 5))
    # Plain URL (the PAT goes in via _git's env): also scrubs a PAT embedded by older versions.
    _git(["remote", "set-url", "origin", f"https://github.com/{DATA_REPO_SLUG}.git"], data_dir)
    rel_paths = [str(p) for p in paths]
    _git(["add", "--all", "--", *rel_paths], data_dir)
    status = _git(["status", "--porcelain", "--", *rel_paths], data_dir)
    if not status.stdout.strip():
        return False
    _git(["commit", "-m", message], data_dir)
    _git(["push", "origin", branch], data_dir, pat=pat)
    return True
