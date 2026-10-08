---
name: republish
description: Publish whatever the pipeline's Parser main has that Data doesn't yet - gate (no active run, clean checkouts, something new to publish), fast-forward the pipeline's Parser checkout + venv, return the Parser dev checkout to main (deleting a patch branch), start wrf-orchestrator@<version>, and verify the new run's parse warnings. Use when the user runs /republish [version], or asks to republish / re-run the pipeline after a parser change merged (a patch-day fix or any other Parser PR). Replaces /merged for Parser PRs.
---

# republish

Re-runs the pipeline so that Parser changes merged since the last publish reach
Data. On patch day it is step 7 of `PATCH_DAY.md`; it works the same after any
Parser PR. The pipeline runs whatever is checked out in
`$REPOS_DIR/WRFrontiersDB-Parser`, so that checkout must be on an up-to-date,
clean `main` before the run.

Argument: the game version (`yyyy-mm-dd[-N]`). Optional; defaults to the version
Data currently publishes (`current/version.txt`), which is the usual case.

Paths (`$WRF_ROOT`, `$REPOS_DIR`: the pipeline's options, see README):
- `P` - the pipeline's Parser checkout: `$REPOS_DIR/WRFrontiersDB-Parser`
- `D` - the Parser dev checkout the change was developed in. Where that is comes
  from the machine's own instructions; it may be `P` itself.
- Other pipeline repos: `$REPOS_DIR/{WRFrontiersDB-Orchestrator,WRFrontiers-Exporter,WRFrontiersDB-Site}`

## 1. Gates + update the pipeline

```bash
P=$REPOS_DIR/WRFrontiersDB-Parser
D=<Parser dev checkout>
REPOS_DIR=$REPOS_DIR bin/republish-prep --dev-checkout "$D" [<version>]
V=<the VERSION: line it printed>
```

The script stops (`STOP:` line, exit 1), before changing anything, when:
- a pipeline run is active (don't stack runs);
- `P` or `D` is dirty;
- `P` is on a branch other than `main` (or `patch/$V` when `D` is `P`);
- there is nothing new to publish: Parser `origin/main` is the commit Data's last
  parse came from (the `Parser-Commit:` trailer of Data's last
  "Update current to version" commit, or for older commits its
  `Parser commit: '<subject> - <date>'` line).

On a `STOP:`, tell the user; don't clean or switch anything yourself. If they
want to publish with nothing new in the Parser (e.g. to pick up Site or Exporter
changes), rerun the script with `--force`.

Otherwise it prints a `NEW:` line per Parser commit not yet published (what this
run will publish), returns `D` to `main` (detached at `origin/main` when `D` is
not `P`), deletes `patch/$V` if it exists, fast-forwards `P`, and runs
`pip install -r requirements.txt` in `P`'s venv (every time: a new requirement
otherwise silently degrades the output).

It then prints a `BEHIND:` line for each other pipeline repo (Orchestrator,
Exporter, Site) that is off `main`, dirty or behind `origin/main`. Pulling those
deploys the user's merged work, so list them and **ask** whether to fast-forward
them before the run; don't decide for them. For the Orchestrator, a pull also
needs `.venv/bin/pip install -q -r requirements.txt`. The SITE stage runs
`npm ci` itself.

Leave `$WRF_ROOT/dev` alone; the next `tools/patch_day.sh init` resets it.

## 2. Run

Invoking this skill is the user's go-ahead to publish; no extra confirmation is
needed unless step 1 raised a question.

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
    --decisions "$D/decisions/$V.json" | grep -E '^(### |- decision:|\| `)'
```

Passing `--decisions` points the report at the dev checkout's local decisions
file (gitignored), when one exists for `$V`. The republished run is
the newest completed parse, so the script marks every `approved` group it no
longer produces as `done`. If one is still present, it stays `approved`: the fix
didn't take. If `$D/decisions/$V.json` doesn't exist (the triage was done without
the file), drop the flag.

## 3. Report

Tell the user: the Parser commits published (the `NEW:` lines), the run result (COMPLETE / FAILED at <stage>), the parse group count
before -> after, the decision statuses (how many `done`, and any `approved` group
that is still present), and any group still left (expected: only the `deferred`
ones). A failed stage after PARSE means the data was pushed but the site wasn't
rebuilt; point at that stage's log. The run-report email goes out on its own.
