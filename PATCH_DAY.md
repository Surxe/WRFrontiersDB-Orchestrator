# Patch day

What happens when War Robots Frontiers patches, what the pipeline does on its
own, and the human-in-the-loop process for the part that needs you: teaching
the parser the patch's new data. Everything runs on the **home-server**; you
work on it from your PC through **VS Code Remote-SSH**.

```
probe (every 20 min) -> wrf-orchestrator@<version> -> EXPORT -> PARSE+PUSH -> RELEASES -> SITE -> SITE-DEPLOY
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
- Every run ends with the **run-report email**: per-step WARNING/ERROR counts,
  then links to every log under `/srv/dev/wrf/logs/<run-timestamp>/`.

Because the data is already live, the parser work below is about making the
*next* publish complete. There's no deadline beyond "before players notice gaps".

## 2. Read the email

| Email says | Meaning | Do |
| --- | --- | --- |
| All counts 0, COMPLETE | Parser covered the patch | Nothing |
| `parse` warnings/errors > 0, COMPLETE | Data published with gaps | Sections 3-7 |
| `FAILED at <STAGE>` | A stage crashed | Read that stage's log (`NN-<stage>.log`). A failure **after** PARSE means the data was already pushed but later stages (site) didn't run. Fix, then [republish](#7-republish). |
| `PREFLIGHT FAILED` | Missing secret/venv/mount | Read `01-preflight.log` |

The warning count on its own tells you little: on 2026-09-29, 1,500 lines were
13 distinct issues, 1,488 of them one repeated message. Section 4 groups them.

## 3. Open the workspace (VS Code Remote-SSH)

One-time setup: an SSH key for `dev@home-server` in `~/.ssh/authorized_keys`,
a `Host home-server` entry in your PC's `~/.ssh/config`, and the **Remote - SSH**
extension. Install the Python extension "in SSH: home-server".

Each patch:

1. `Ctrl+Shift+P` -> **Remote-SSH: Connect to Host** -> `home-server`.
2. **File -> Open Folder** -> `/srv/dev/repos/WRFrontiersDB-Parser-dev` (the
   parser **dev worktree**, not the pipeline's clone at `.../WRFrontiersDB-Parser`).
3. **File -> Add Folder to Workspace** -> `/srv/dev/wrf/dev/parsed-review` (the
   parsed-output review repo; appears after `init`). Optionally also
   `/srv/dev/wrf/logs`. **File -> Save Workspace As...** so next time it's one
   click.

When you're done, close the remote window: the VS Code server uses ~0.5 GB on a
7 GB box that also runs the Valheim VM, and a scratch parse peaks at ~2.6 GB.

## 4. Triage with Claude

In the VS Code terminal (it runs on the server):

```bash
cd /srv/dev/repos/WRFrontiersDB-Parser-dev
claude
> /patch-warnings 2026-09-29
```

The `patch-warnings` skill (in the Parser repo, `.claude/skills/`) will:

1. Put the dev worktree on a `patch/<version>` branch and run
   `tools/patch_day.sh init <version>`. That points the worktree's `.env` at the
   patch's export and creates the review repo, whose `baseline` commit is the
   pipeline's parsed output.
2. Group the pipeline's parse log with `tools/warning_report.py`, in the
   order **error -> warning -> unknown-property**. Plain `warning`s are the
   likely regressions: an existing parser check tripped on changed data. The
   report is written to `reports/latest.md` in the dev worktree (gitignored).
   Open it and press `Ctrl+Shift+V` for the preview. It has a summary table, and
   each group links to the owning parser line and the export JSON, which open in
   the editor, plus the asset viewer. Leave the preview open: every scratch
   parse rewrites the file and the preview refreshes.
3. Do a sanity scratch parse with no edits. The review diff should be empty.
   If it isn't, the pipeline's environment differs from the dev worktree's (see
   [Troubleshooting](#troubleshooting)).
4. Read the export slice behind each group and write a proposal for each one
   into the decisions file (below).

**Decisions file.** The report keeps `decisions/<version>.json` in the dev
worktree in sync with the log. The file is **gitignored and local only**, so
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

To look at the data yourself, run `tools/patch_day.sh viewer`. VS Code forwards
port 8765, so the report's `[viewer]` links open on your PC. Paste any warning
line or `/Game/...` path into the viewer to follow references.

## 5. Review the output diff

Claude implements the approved rows, then iterates `tools/patch_day.sh parse`
(which also rewrites `reports/latest.md`)
(scratch parse into `/srv/dev/wrf/dev/parsed`, synced into the review repo)
until those groups are gone and nothing NEW appeared.

**Gate 2 - you review.** In VS Code's **Source Control** panel, pick the
`parsed-review` repo. Every changed parsed file is listed; click one for a
side-by-side diff of what the parser change did to the published JSON since the
last checkpoint. `Objects/*.json` are pretty-printed. `Models/*.json` are
one-line, so use `git -C /srv/dev/wrf/dev/parsed-review diff --word-diff -- Models/<file>`
or ask Claude to summarise them. When you're happy, say so. Claude runs
`tools/patch_day.sh checkpoint "<msg>"`, so the next iteration's diff shows
only the new changes. `tools/patch_day.sh status` shows the checkpoints and the
total change vs. baseline.

## 6. PR and merge

Claude commits to `patch/<version>` and opens the PR with the `pr` skill (body
covering: properties handled, skipped + why, fixes, open items,
verification). Review and merge it on GitHub as usual.

**Don't run `/merged` for a parser patch PR.** It would try to check out `main` in
the dev worktree, and `main` is held by the pipeline's clone. `/republish-patch`
does the post-merge cleanup instead.

## 7. Republish

From the Orchestrator repo:

```bash
cd /srv/dev/repos/WRFrontiersDB-Orchestrator
claude
> /republish-patch 2026-09-29
```

The skill checks that the PR merged and that no run is active. It then
fast-forwards the pipeline's Parser clone to `main` and syncs its venv, parks
the dev worktree at `origin/main` (deleting the patch branch), and re-runs
`sudo systemctl start wrf-orchestrator@<version>.service`. Nothing is re-downloaded
or re-exported, because without `--force-*` those steps skip output that
already exists. The run re-parses, re-pushes `current/`, re-runs RELEASES
(idempotent) and rebuilds/deploys the site. It then groups the new run's parse
log against the dev worktree's `decisions/<version>.json`. That run is the
newest completed parse, so groups it no longer produces are confirmed `done`, and
the report shows what's left (normally just the deferred ones). You also get
the usual email.

By hand:

```bash
git -C /srv/dev/repos/WRFrontiersDB-Parser pull --ff-only
/srv/dev/repos/WRFrontiersDB-Parser/.venv/bin/pip install -q -r /srv/dev/repos/WRFrontiersDB-Parser/requirements.txt
sudo systemctl start --no-block wrf-orchestrator@2026-09-29.service
sudo journalctl -u wrf-orchestrator@2026-09-29 -f
```

## Doing it without Claude

Everything the skill does is plain tooling in the Parser dev worktree:

```bash
cd /srv/dev/repos/WRFrontiersDB-Parser-dev
git fetch origin && git switch -c patch/2026-09-29 origin/main
tools/patch_day.sh init 2026-09-29
.venv/bin/python tools/warning_report.py              # group the latest pipeline run's parse log -> reports/latest.md
                                                      #   + decisions/2026-09-29.json (edit status/proposal there)
tools/patch_day.sh parse                              # after each edit: scratch parse + diff + reports/latest.md
tools/patch_day.sh report                             # re-write the scratch parse's report
tools/patch_day.sh checkpoint "handled TeslaFeed"     # accept the current diff
tools/patch_day.sh viewer                             # asset viewer on :8765
```

## Rules of thumb

- **The parser is the only repo patch day changes on the server**, and only in
  the dev worktree. The Orchestrator, Exporter, Site and Data repos are developed
  on the home PC; the server's clones of them only move by pulling merged `main`.
- The pipeline clones must stay on a clean `main`: the service runs whatever is
  checked out.
- Scratch parses never push. Publishing is the pipeline's job.
- `tools/patch_day.sh init` resets the review repo. Re-run it only for a new version.
- `decisions/` never goes to the remote (gitignored). It's the only record of
  deferred items between patches, so include the dev worktree's `decisions/` in
  your own backups.

## Troubleshooting

- **Sanity scratch parse shows a diff with no parser edits.** The pipeline ran
  with a different environment than the dev worktree. On 2026-09-29 the pipeline
  Parser venv lacked `zstandard` (a newly added requirement), so every
  model's `meshes` came out empty. The only trace was 370 DEBUG lines. Fix with
  `pip install -r requirements.txt` in the pipeline clone's venv (republish does
  this), then republish.
- **Scratch parse killed / box sluggish.** Memory: a parse peaks ~2.6 GB. Don't
  run one while the pipeline is running (`systemctl is-active 'wrf-orchestrator@*'`).
- **`patch_day: on 'main' ... looks like the pipeline's clone`.** You ran the
  tool from `/srv/dev/repos/WRFrontiersDB-Parser`; `cd` to `...-Parser-dev`.
- **The report says `baseline: none`.** No earlier completed pipeline parse
  exists to compare against, so there are no NEW markers. Grouping still works.
