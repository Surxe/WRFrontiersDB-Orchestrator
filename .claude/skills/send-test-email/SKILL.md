---
name: send-test-email
description: Send a test run-report email (the patch-day report, with the run's logs attached) built from an existing run's logs, without running the pipeline. Uses the wrf-orchestrator@ unit's own environment so the SMTP secrets are injected, never read. Use when the user runs /send-test-email [run-dir], or asks to test / trigger / resend the run-report email after changing the emailer (src/alerts.py, src/report.py).
---

# send-test-email

Sends one run-report email through the real path (`alerts.send_report`: compose,
log attachments, SMTP), rebuilt from a finished run's log dir by
`src/send_test_email.py`. The patch shows as `TEST` and the result as
`TEST (from <run-dir>)`, so it can't be mistaken for a real run.

Argument (optional): a run dir (`$WRF_ROOT/logs/<run-timestamp>`). Default: the
newest run under `$WRF_ROOT/logs`.

## Never start the unit itself

`wrf-orchestrator@<version>` runs the WHOLE patch-day pipeline (export, parse,
push data, deploy site). Don't start it to test the email. Borrow only its
environment with `systemd-run`, as below.

## Never read the secrets

The SMTP secrets live in the unit's `EnvironmentFile`, which you can't (and
mustn't) read. `systemd-run -p EnvironmentFile=` hands them straight to the
process. The script's option logging masks sensitive values as `***HIDDEN***`;
still, show only the tail of the output.

## 1. Gates

```bash
systemctl list-units 'wrf-orchestrator@*' --state=activating,active --no-legend
systemctl show -p EnvironmentFiles wrf-orchestrator@x.service
```

- An orchestrator run is active -> STOP. Its run dir is still being written, and
  the real report email is on its way anyway.
- No `EnvironmentFiles=` path -> STOP and tell the user; the unit isn't installed
  on this machine, so there are no secrets to send with.

## 2. Send

Run as the unit's user, from the unit's working directory (this repo), with the
unit's EnvironmentFile. `WRF_ROOT` is the pipeline option (see README; default
`/srv/dev/wrf`); runs log to `$WRF_ROOT/logs`, as the unit's `--log-dir` says.

```bash
U=wrf-orchestrator@x.service
ENVFILE=$(systemctl show -p EnvironmentFiles $U --value | sed -E 's/ \(.*//')
USR=$(systemctl show -p User $U --value); GRP=$(systemctl show -p Group $U --value)
REPO=$(systemctl show -p WorkingDirectory $U --value)
WRF_ROOT=/srv/dev/wrf   # or this machine's WRF_ROOT
sudo systemd-run --quiet --wait --pipe --collect --uid="$USR" --gid="$GRP" \
  -p EnvironmentFile="$ENVFILE" -p WorkingDirectory="$REPO" \
  "$REPO/.venv/bin/python" src/send_test_email.py --log-dir "$WRF_ROOT/logs" [RUN_DIR] \
  2>&1 | tail -3; echo "exit ${PIPESTATUS[0]}"
```

## 3. Report

- `Run report emailed to ...` and exit 0 -> sent. Tell the user which run dir it
  was built from and what to expect: the subject `WRFrontiersDB TEST - TEST (from
  <run-dir>): ...`, and the attachments (each log as text; one `<run-dir>-logs.zip`
  if the logs total over 10 MB raw; none if over 15 MB zipped).
- `Email not configured` -> the EnvironmentFile lacks `SMTP_USER` /
  `SMTP_PASSWORD` / `EMAIL_TO`. Tell the user; don't try to find or set them.
- `SMTP authentication rejected` -> the App Password is wrong or revoked. Tell
  the user; it's theirs to rotate.
- Exit 2 / `No run dir found` -> there's no run to build from; name the dir looked in.
