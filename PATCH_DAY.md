# Patch day

What happens when War Robots Frontiers patches, what the pipeline does on its
own, and the human-in-the-loop process for the part that needs you: teaching
the parser the patch's new data. Everything runs on the machine that hosts the
pipeline. Paths use the pipeline's two knobs, `$WRF_ROOT` and `$REPOS_DIR` (see
[README](README.md#options)). The pipeline runs the Parser checked out at
`$REPOS_DIR/WRFrontiersDB-Parser`; the **Parser dev checkout** below is wherever
the machine's own instructions say to develop the Parser (possibly that same
directory).

```
probe (every 20 min) -> wrf-orchestrator@<version> -> EXPORT -> PARSE+PUSH -> INDEX -> SITE -> SITE-DEPLOY
                                                                 |
                                         warnings/errors are logged, data is still published
                                                                 v
                          run-report email -> triage + parser fix (this runbook) -> PR -> republish
```

## 1. What runs by itself

- `hs-wrf-update-probe.timer` spots a new Steam manifest and starts
  `wrf-orchestrator@<version>.service`, which runs `src/run.py --patch-day`
  (see [README](README.md#pipeline) for the stages).
- **Warnings never block publishing.** A parse that completes is pushed to
  WRFrontiersDB-Data even with thousands of warnings, and so is one with
  `logger.error` lines. Only an exception (a stage exiting non-zero) stops the
  run, and with it every later stage.
- Every run ends with the **run-report email**: per-step WARNING / ERROR /
  UNKNOWN_PROPERTY counts (the last is the parser's level for new game data it
  neither parses nor skips; counted only, not listed inline), then the parse
  log's **warning groups** (from the Parser's `tools/warning_report.py`; NEW marks a
  group the last completed parse didn't have), then links to every log under
  `$WRF_ROOT/logs/<run-timestamp>/`, with the logs and the grouped report
  (`parse-warnings.md`) attached. The groups are a preview only - `/patch-warnings`
  re-runs the report in the Parser dev worktree, where decisions are tracked.

Because the data is already live, the parser work below is about making the
*next* publish complete. There's no deadline beyond "before players notice gaps".

## 2. Read the email

| Email says | Meaning | Do |
| --- | --- | --- |
| All counts 0, COMPLETE | Parser covered the patch | Nothing |
| `parse` warnings/errors/unknown properties > 0, COMPLETE | Data published with gaps | Sections 3-7 |
| `FAILED at <STAGE>` | A stage crashed | Read that stage's log (`NN-<stage>.log`). A failure **after** PARSE means the data was already pushed but later stages (site) didn't run. Fix, then [republish](#7-republish). |
| `PREFLIGHT FAILED` | Missing secret/venv/mount | Read `01-preflight.log` |

The counts on their own tell you little: on 2026-09-29, 1,500 lines were
13 distinct issues, 1,488 of them one repeated message. Section 4 groups them.

## 3. Open the workspace

Open the Parser dev checkout in your editor, and add
`$WRF_ROOT/dev/parsed-review` (the parsed-output review repo; appears after
`init`) and optionally `$WRF_ROOT/logs`. A scratch parse peaks at ~2.6 GB of
memory.

## 4. Triage with Claude

```bash
cd <Parser dev checkout>
claude
> /patch-warnings 2026-09-29
```

The `patch-warnings` skill (in the Parser repo, `.claude/skills/`) will:

1. Put the dev checkout on a `patch/<version>` branch and run
   `tools/patch_day.sh init <version>`. That points the dev checkout's `.env` at the
   patch's export and creates the review repo, whose `baseline` commit is the
   pipeline's parsed output.
2. Group the pipeline's parse log with `tools/warning_report.py`, in the
   order **error -> warning -> unknown-property**. Plain `warning`s are the
   likely regressions: an existing parser check tripped on changed data. The
   report is written to `reports/latest.md` in the dev checkout (gitignored).
   Open it and press `Ctrl+Shift+V` for the preview. It has a summary table, and
   each group links to the owning parser line and the export JSON, which open in
   the editor, plus the asset viewer. Leave the preview open: every scratch
   parse rewrites the file and the preview refreshes.
3. Do a sanity scratch parse with no edits. The review diff should be empty.
   If it isn't, the pipeline's environment differs from your scratch parse's (see
   [Troubleshooting](#troubleshooting)).
4. Read the export slice behind each group and write a proposal for each one
   into the decisions file (below).

**Decisions file.** The report keeps `decisions/<version>.json` in the dev
checkout in sync with the log. The file is **gitignored and local only**, so
back it up yourself if you want it kept. Every group has a stable id
(`u-`/`w-`/`e-` + 8 hex, shown in the report) and one entry with `status`,
`proposal`, `reason`, `confidence`, `notes`. New groups arrive as `undecided`.
Claude moves them to `proposed`, and you set `approved` or `deferred`. The
script itself marks `approved` -> `done` once a newer completed parse no longer
produces the group, and back to `approved` (with a note) if it reappears. A
decision made for the same id in an earlier patch shows as "previously". The
report renders status and proposal for every group, plus a *Not in this log*
table, so `reports/latest.md` also tracks the patch's progress.

**Gate 1 - you decide.** Claude stops once its proposals are in the file and the
report shows them (`value`, `parse via <fn>`, `skip #reason`, `fix logic`,
`elevate to error`, `needs user`). Review them in the preview, then set
`status` to `approved` or `deferred` in the JSON, rewriting `proposal` if you
disagree, or tell Claude in chat and it sets them. Claude implements only
`approved` entries. The parse-vs-skip conventions are in the Parser's `CLAUDE.md` (*Key maps: parse or
skip*). The log-level rules are in its `STANDARDS.md` (*Choosing a Log Level*):
anything that silently drops published data should be `logger.error`, not a
warning.

To look at the data yourself, run `tools/patch_day.sh viewer` (port 8765; the
report's `[viewer]` links point there). Paste any warning
line or `/Game/...` path into the viewer to follow references.

## 5. Review the output diff

Claude implements the approved rows, then iterates `tools/patch_day.sh parse`
(which also rewrites `reports/latest.md`)
(scratch parse into `$WRF_ROOT/dev/parsed`, synced into the review repo)
until those groups are gone and nothing NEW appeared.

**Gate 2 - you review.** In VS Code's **Source Control** panel, pick the
`parsed-review` repo. Every changed parsed file is listed; click one for a
side-by-side diff of what the parser change did to the published JSON since the
last checkpoint. `Objects/*.json` are pretty-printed. `Models/*.json` are
one-line, so use `git -C $WRF_ROOT/dev/parsed-review diff --word-diff -- Models/<file>`
or ask Claude to summarise them. When you're happy, say so. Claude runs
`tools/patch_day.sh checkpoint "<msg>"`, which accepts the step on both sides:
it commits the parser code (`src/`, `tests/`) on `patch/<version>` and the
review repo with the same message, links them by hash, and lists the decision
ids that became `done` (also stamped into the decisions file as `checkpoint` /
`commit`). It refuses code on another branch, or code edited after the last
scratch parse. The next iteration's diff then shows only the new changes.
`tools/patch_day.sh status` shows the checkpoints and the total change vs.
baseline.

## 6. PR and merge

The code is already on `patch/<version>`, one commit per checkpoint, so the PR
can be reviewed step by step. Claude opens it with the `pr` skill (body built
from the decisions file: properties handled, skipped + why, fixes, open items,
verification). Review and merge it on GitHub as usual (squash or not is your
call).

`/republish-patch` does the post-merge cleanup (dev checkout back to `main`,
patch branch deleted) as part of republishing.

## 7. Republish

From the Orchestrator repo:

```bash
cd $REPOS_DIR/WRFrontiersDB-Orchestrator
claude
> /republish-patch 2026-09-29
```

The skill checks that the PR merged and that no run is active. It then returns
the dev checkout to `main` (deleting the patch branch), fast-forwards the
pipeline's Parser checkout and syncs its venv, and re-runs
`sudo systemctl start wrf-orchestrator@<version>.service`. Nothing is re-downloaded
or re-exported, because without `--force-*` those steps skip output that
already exists. The run re-parses, re-pushes `current/`, re-runs INDEX
(idempotent) and rebuilds/deploys the site. It then groups the new run's parse
log against the dev checkout's `decisions/<version>.json`. That run is the
newest completed parse, so groups it no longer produces are confirmed `done`, and
the report shows what's left (normally just the deferred ones). You also get
the usual email.

By hand:

```bash
P=$REPOS_DIR/WRFrontiersDB-Parser
git -C $P pull --ff-only    # P must already be on a clean main
$P/.venv/bin/pip install -q -r $P/requirements.txt
sudo systemctl start --no-block wrf-orchestrator@2026-09-29.service
sudo journalctl -u wrf-orchestrator@2026-09-29 -f
```

## Doing it without Claude

Everything the skill does is plain tooling in the Parser dev checkout:

```bash
cd <Parser dev checkout>
git fetch origin && git switch -c patch/2026-09-29 origin/main
tools/patch_day.sh init 2026-09-29
.venv/bin/python tools/warning_report.py              # group the latest pipeline run's parse log -> reports/latest.md
                                                      #   + decisions/2026-09-29.json (edit status/proposal there)
tools/patch_day.sh parse                              # after each edit: scratch parse + diff + reports/latest.md
tools/patch_day.sh report                             # re-write the scratch parse's report
tools/patch_day.sh checkpoint "handled TeslaFeed"     # accept the step: commit code + review diff, linked
tools/patch_day.sh viewer                             # asset viewer on :8765
```

## Rules of thumb

- **The parser is the only repo patch day changes.**
- The pipeline runs whatever is checked out at `$REPOS_DIR/WRFrontiersDB-Parser`;
  it must be on a clean, current `main` when a run starts.
- Scratch parses never push. Publishing is the pipeline's job.
- `tools/patch_day.sh init` resets the review repo. Re-run it only for a new version.
- `decisions/` never goes to the remote (gitignored). It's the only record of
  deferred items between patches, so include the dev checkout's `decisions/` in
  your own backups.

## Troubleshooting

- **Sanity scratch parse shows a diff with no parser edits.** The pipeline ran
  with a different environment than your scratch parse. On 2026-09-29 the pipeline
  Parser venv lacked `zstandard` (a newly added requirement), so every
  model's `meshes` came out empty. The only trace was 370 DEBUG lines. Fix with
  `pip install -r requirements.txt` in the pipeline Parser's venv (republish does
  this), then republish.
- **Scratch parse killed / box sluggish.** Memory: a parse peaks ~2.6 GB. Don't
  run one while the pipeline is running (`systemctl is-active 'wrf-orchestrator@*'`).
- **Is the new data live?** `bin/wrf-deployed` shows which Data commit the Site
  and the Visualizer serve (from their `/deploy.json`) and how far behind Data
  `main` each is. If the Site is behind after a run, SITE-DEPLOY didn't deploy:
  check the run report's `site-ci` step. The output links the run that built
  what's live.
- **The report says `baseline: none`.** No earlier completed pipeline parse
  exists to compare against, so there are no NEW markers. Grouping still works.
