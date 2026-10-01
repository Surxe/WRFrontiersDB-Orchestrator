---
name: republish-patch
description: After a parser patch PR (patch/<version>) is merged, bring the home-server pipeline up to date and re-run the orchestrator for that version so the fixed parse is published - fast-forward the pipeline's Parser clone + venv, park the parser dev worktree, start wrf-orchestrator@<version>, and verify the new run's parse warnings. Use when the user runs /republish-patch <version> or asks to republish / re-run the pipeline after a parser fix merged. Replaces /merged for parser patch PRs.
---

# republish-patch

Step 7 of `PATCH_DAY.md`. The parser fix for `<version>` has merged; publish it.
The pipeline's clones only ever move by fast-forwarding merged `main`, and the
service runs whatever they have checked out.

Argument: the patch version (`yyyy-mm-dd[-N]`). Required.

Paths:
- Pipeline Parser clone: `/srv/dev/repos/WRFrontiersDB-Parser`
- Parser dev worktree: `/srv/dev/repos/WRFrontiersDB-Parser-dev`
- Other pipeline clones: `/srv/dev/repos/{WRFrontiersDB-Orchestrator,WRFrontiers-Exporter,WRFrontiersDB-Site}`

## 1. Gates (stop on any failure and tell the user)

```bash
V=<version>
P=/srv/dev/repos/WRFrontiersDB-Parser
REPO=$(git -C $P remote get-url origin | sed -E 's#https://([^@]*@)?github.com/##; s#\.git$##')
gh pr list -R "$REPO" --state merged --head "patch/$V" --json number,title,mergedAt,url
systemctl list-units 'wrf-orchestrator@*' --state=activating,active --no-legend
git -C $P status --porcelain; git -C $P rev-parse --abbrev-ref HEAD
```

- No merged PR for `patch/$V` -> STOP. If the fix went in under another branch
  name, ask the user for the PR and check that it's merged instead.
- An orchestrator run is active -> STOP. Don't stack runs.
- Pipeline Parser clone not on `main`, or dirty -> STOP. Don't clean it yourself;
  something edited the pipeline clone and the user needs to know.

## 2. Update the pipeline

```bash
git -C $P pull --ff-only
$P/.venv/bin/pip install -q -r $P/requirements.txt
```

`pip install` every time: a new requirement (like `zstandard` for the model parser) otherwise
silently degrades the pipeline's output. Then check the other pipeline clones:

```bash
for r in WRFrontiersDB-Orchestrator WRFrontiers-Exporter WRFrontiersDB-Site; do
  d=/srv/dev/repos/$r; git -C $d fetch -q origin
  echo "$r: $(git -C $d rev-parse --abbrev-ref HEAD), behind origin/main by $(git -C $d rev-list --count HEAD..origin/main), dirty $(git -C $d status --porcelain | wc -l)"
done
```

Those repos are developed on the home PC, so pulling them deploys the user's merged
work. If any is behind or off `main`, list them and **ask** whether to
fast-forward them before the run; don't decide for him. For the Orchestrator,
a pull also needs `.venv/bin/pip install -q -r requirements.txt`. The SITE stage
runs `npm ci` itself.

## 3. Park the dev worktree

Only when the merge gate passed:

```bash
W=/srv/dev/repos/WRFrontiersDB-Parser-dev
git -C $W status --porcelain          # must be empty - else STOP and ask
git -C $W fetch --prune origin
git -C $W switch --detach origin/main
git -C $W branch -D "patch/$V"        # -D: squash merges aren't ancestors
```

Leave `/srv/dev/wrf/dev` alone; the next `tools/patch_day.sh init` resets it.

## 4. Run

Invoking this skill is the user's go-ahead to publish; no extra confirmation is
needed unless step 2 raised a question.

```bash
U="wrf-orchestrator@$V.service"
BEFORE=$(systemctl show "$U" -p InvocationID --value)   # empty if it never ran
sudo systemctl start --no-block "$U"
```

The unit runs `--patch-day` without `--force-*`, so download, mapper and
BatchExport skip their existing output. What actually runs is parse -> push ->
releases -> site -> deploy.

Wait for **this** run to finish, in a background Bash call (or the Monitor tool),
not a foreground sleep loop:

```bash
n=0; until [ "$(systemctl show "$U" -p InvocationID --value)" != "$BEFORE" ]; do
  n=$((n+1)); [ $n -gt 24 ] && { echo "STOP: run never started"; exit 1; }; sleep 5; done
while [ "$(systemctl show "$U" -p ActiveState --value)" = activating ]; do sleep 15; done
```

Don't use `systemctl is-active`: the unit is `Type=oneshot`, so it stays
`activating` for the whole run, `is-active` reports that as not-active, and an
`until ! is-active` loop exits at once. `Result` then still describes the
*previous* run, so a run in progress looks like a success. Right after a
`--no-block` start the state can also still read `inactive` while the job is
queued. Waiting for the `InvocationID` to change, then for `ActiveState` to
leave `activating`, avoids both. Then:

```bash
systemctl show "$U" -p ActiveState -p Result -p ExecMainStatus -p InvocationID
RUN=$(ls -d /srv/dev/wrf/logs/*/ | sort | tail -1)
tail -n 15 "$RUN/run.log"
$P/.venv/bin/python $P/tools/warning_report.py "$RUN" --summary
$P/.venv/bin/python $P/tools/warning_report.py "$RUN" --stdout \
    --decisions "$W/decisions/$V.json" | grep -E '^(### |- decision:|\| `)'
```

Passing `--decisions` points the report at the dev worktree's local decisions file
(gitignored there; never copy it into the pipeline clone). The republished run is
the newest completed parse, so the script marks every `approved` group it no
longer produces as `done`. If one is still present, it stays `approved`: the fix
didn't take. If `$W/decisions/$V.json` doesn't exist (the triage was done without
the file), drop the flag.

## 5. Report

Tell the user: the run result (COMPLETE / FAILED at <stage>), the parse group count
before -> after, the decision statuses (how many `done`, and any `approved` group
that is still present), and any group still left (expected: only the `deferred`
ones). A failed stage after PARSE means the data was pushed but the site wasn't
rebuilt; point at that stage's log. The run-report email goes out on its own.
