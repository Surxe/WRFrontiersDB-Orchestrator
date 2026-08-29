"""Secret loading.

Non-secret config lives in the repo's .env (loaded by optionsconfig).

Secrets do NOT live anywhere `dev` can read them. `dev` is the repo-writable,
AI-touched account, so the Steam password and the data-repo PAT must not sit in
`~dev`. Instead the real secret file is ethan-owned and mode 600:

    ~ethan/.config/wrf-orchestrator/secrets.env    (chmod 600, ethan-only)

mirroring ~ethan/.config/steam-price-tracker/smtp.env. The pipeline runs as
`dev`, so it cannot read that file directly; an ethan-owned launcher sources it
and injects the three values into the `dev` process's ENVIRONMENT at launch
(e.g. `sudo -u dev --preserve-env=STEAM_USERNAME,STEAM_PASSWORD,GH_DATA_REPO_PAT`),
so `dev` only ever sees them transiently in a running process, never at rest.

Accordingly, load_secrets() reads secrets from the ENVIRONMENT by default. Only
if WRF_SECRETS_ENV points at a file the current user can actually read (e.g.
ethan running the pipeline directly) do we also overlay that file — never
overriding what the launcher already injected.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# The secret option names, for validation messages.
SECRET_KEYS = ("STEAM_USERNAME", "STEAM_PASSWORD", "GH_DATA_REPO_PAT")

# Where the ethan-owned secret file is expected to live (documented, not read by
# dev). Used only for help text.
CANONICAL_SECRETS_PATH = "~ethan/.config/wrf-orchestrator/secrets.env"


def load_secrets() -> None:
    """Overlay secrets from an optional readable file, non-overriding.

    Secrets normally arrive already in the environment (injected by the launcher).
    If WRF_SECRETS_ENV names a file the current user can read, overlay it too, but
    never override a value the environment already carries.
    """
    override_path = os.environ.get("WRF_SECRETS_ENV")
    if override_path:
        p = Path(override_path).expanduser()
        if p.is_file() and os.access(p, os.R_OK):
            load_dotenv(p, override=False)


def missing_secrets(keys: tuple[str, ...]) -> list[str]:
    """Of the given secret keys, which are absent or empty in the environment."""
    return [k for k in keys if not os.environ.get(k)]
