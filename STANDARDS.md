# Standards

Mirrors the conventions of the sibling repos (WRFrontiers-Exporter,
WRFrontiersDB-Parser) so the toolchain reads as one system.

## Options

* Uses `optionsconfig`: `options_schema.py` holds `OPTIONS_SCHEMA`; `build/docs.py`
  regenerates `.env.example` and the README options block.
* Value priority (descending): **argument > parameter (.env) > default**.
* `SHOULD_` flags gate stages; `FORCE_` flags override skip/idempotency guards.
* If every `SHOULD_` option resolves to `False`, they are all treated as `True`
  (running with no flags runs the whole pipeline).

## Paths

* Folder path → `dir`; executable file path → `cmd`; other file path → `file`.
* **Every stage path derives from `WRF_ROOT`** (see `src/repos.py`). Sub-repos are
  never pointed at hand-written paths by the orchestrator — the derivation is the
  single source of truth, which is what keeps the Exporter's output and the
  Parser's input from drifting apart.

## Secrets

* Never in the repo. Real file: `~dev/.config/wrf-orchestrator/secrets.env`
  (`chmod 600`, dev-owned, hand-maintained). Repo ships only `secrets.env.example`.
* Secrets reach sub-repos via the subprocess **environment**, never as CLI args
  (so they never appear in `ps`). Non-secret values are passed as explicit args.

## Logging

* Levels: `TRACE, DEBUG, INFO, WARNING, ERROR, CRITICAL`.
* Every stage's child process is streamed live to the console **and** teed to
  `LOG_DIR/<run-timestamp>/<NN-stage>.log`. Children run with `PYTHONUNBUFFERED=1`
  so output is never swallowed by block buffering.

## Case

* `snake_case` for variables/functions, `PascalCase` for classes,
  `SCREAMING_SNAKE_CASE` for schema keys / env vars.
* Docs are `ALLCAPS.md`; other files `snake_case`.
