---
name: republish-patch
description: After a parser patch PR (patch/<version>) is merged, bring the pipeline up to date and re-run the orchestrator for that version so the fixed parse is published - switch the Parser back to main (deleting the patch branch), fast-forward it + its venv, start wrf-orchestrator@<version>, and verify the new run's parse warnings. Use when the user runs /republish-patch <version> or asks to republish / re-run the pipeline after a parser fix merged. Replaces /merged for parser patch PRs.
---

# republish-patch

Step 7 of `PATCH_DAY.md`. The parser fix for `<version>` has merged; publish it.
The pipeline runs whatever is checked out in `$REPOS_DIR`, so the Parser must be
back on an up-to-date, clean `main` before the run.

Argument: the patch version (`yyyy-mm-dd[-N]`). Required.

Paths (`$WRF_ROOT`, `$REPOS_DIR`: the pipeline's options, see README):
- Parser: `$REPOS_DIR/WRFrontiersDB-Parser`
- Other pipeline repos: `$REPOS_DIR/{WRFrontiersDB-Orchestrator,WRFrontiers-Exporter,WRFrontiersDB-Site}`

## 1. Gates (stop on any failure and tell the user)

```bash
V=<version>
P=$REPOS_DIR/WRFrontiersDB-Parser
REPO=$(git -C $P remote get-url origin | sed -E 's#https://([^@]*@)?github.com/##; s#\.git$##')
gh pr list -R "$REPO" --state merged --head "patch/$V" --json number,title,mergedAt,url
systemctl list-units 'wrf-orchestrator@*' --state=activating,active --no-legend
git -C $P status --porcelain; git -C $P rev-parse --abbrev-ref HEAD
```

- No merged PR for `patch/$V` -> STOP. If the fix went in under another branch
  name, ask the user for the PR and check that it's merged instead.
- An orchestrator run is active -> STOP. Don't stack runs.
- Parser dirty, or on a branch other than `main` / `patch/$V` -> STOP. Don't
  clean it yourself; ask the user.

## 2. Update the pipeline

```bash
git -C $P fetch --prune origin
git -C $P switch main
git -C $P pull --ff-only
git -C $P branch -D "patch/$V" 2>/dev/null || true   # -D: squash merges aren't ancestors
$P/.venv/bin/pip install -q -r $P/requirements.txt
```

`pip install` every time: a new requirement (like `zstandard` for the model parser) otherwise
silently degrades the pipeline's output. Then check the other pipeline repos:

```bash
for r in WRFrontiersDB-Orchestrator WRFrontiers-Exporter WRFrontiersDB-Site; do
  d=$REPOS_DIR/$r; git -C $d fetch -q origin
  echo "$r: $(git -C $d rev-parse --abbrev-ref HEAD), behind origin/main by $(git -C $d rev-list --count HEAD..origin/main), dirty $(git -C $d status --porcelain | wc -l)"
done
```

Pulling those repos deploys the user's merged work. If any is behind or off `main`, list them and **ask** whether to
fast-forward them before the run; don't decide for them. For the Orchestrator,
a pull also needs `.venv/bin/pip install -q -r requirements.txt`. The SITE stage
runs `npm ci` itself.

Leave `$WRF_ROOT/dev` alone; the next `tools/patch_day.sh init` resets it.

## 3. Run

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
RUN=$(ls -d $WRF_ROOT/logs/*/ | sort | tail -1)
tail -n 15 "$RUN/run.log"
$P/.venv/bin/python $P/tools/warning_report.py "$RUN" --summary
$P/.venv/bin/python $P/tools/warning_report.py "$RUN" --stdout \
    --decisions "$P/decisions/$V.json" | grep -E '^(### |- decision:|\| `)'
```

Passing `--decisions` points the report at the Parser's local decisions file
(gitignored). The republished run is
the newest completed parse, so the script marks every `approved` group it no
longer produces as `done`. If one is still present, it stays `approved`: the fix
didn't take. If `$P/decisions/$V.json` doesn't exist (the triage was done without
the file), drop the flag.

## 4. Report

Tell the user: the run result (COMPLETE / FAILED at <stage>), the parse group count
before -> after, the decision statuses (how many `done`, and any `approved` group
that is still present), and any group still left (expected: only the `deferred`
ones). A failed stage after PARSE means the data was pushed but the site wasn't
rebuilt; point at that stage's log. The run-report email goes out on its own.
