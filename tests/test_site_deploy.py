"""Unit + integration tests for the SITE build (slug-gen) and SITE-DEPLOY stages.

No external processes run: `run_streamed` is stubbed with a recorder, so npm and
`gh` are never invoked (e2e is exercised separately). These cover the wiring:
  * site.run runs `npm ci` before `sync:slugs` before `build`, and
    short-circuits if install or slugs fail;
  * site_deploy.run dispatches the right `gh workflow run` command, then follows
    the CI run and fails when it doesn't succeed;
  * a successful deploy (only) is recorded in the deploy state file, from the
    run's deploy-record artifact;
  * run.main sequences SITE-DEPLOY after SITE and skips it when a build breaks;
  * --patch-day enables the deploy;
  * preflight fails fast when gh is missing.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Mirror run.py's import wiring: repo root (options_schema) + src (stages).
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import deploy_record  # noqa: E402
import gh_runs  # noqa: E402
import preflight  # noqa: E402
import run  # noqa: E402
from optionsconfig import ArgumentWriter  # noqa: E402
from repos import Repos  # noqa: E402
from stages import site as site_stage  # noqa: E402
from stages import site_deploy as site_deploy_stage  # noqa: E402


def _parse(argv: list[str]) -> argparse.Namespace:
    """Build the same parser run.py's __main__ uses and parse argv."""
    p = argparse.ArgumentParser()
    p.add_argument("--patch-day", action="store_true")
    p.add_argument("--force-patch-day", action="store_true")
    ArgumentWriter().add_arguments(p)
    return p.parse_args(argv)


class Recorder:
    """Stand-in for run_streamed: records (stage, cmd) and returns a chosen rc."""

    def __init__(self, rc_by_stage: dict[str, int] | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.rc_by_stage = rc_by_stage or {}

    def __call__(self, cmd, *, cwd, stage, log_path, env=None) -> int:
        self.calls.append((stage, list(cmd)))
        return self.rc_by_stage.get(stage, 0)

    @property
    def stages(self) -> list[str]:
        return [s for s, _ in self.calls]


class SiteStageUnitTests(unittest.TestCase):
    def test_installs_before_slugs_before_build(self):
        rec = Recorder()
        with mock.patch.object(site_stage, "run_streamed", rec):
            rc = site_stage.run(SimpleNamespace(), _fake_repos(), "2026-08-22", _fake_runlog())
        self.assertEqual(rc, 0)
        self.assertEqual(rec.stages, ["site-install", "site-slugs", "site"])
        self.assertEqual(rec.calls[0][1], ["npm", "ci"])
        self.assertEqual(rec.calls[1][1], ["npm", "run", "sync:slugs"])
        self.assertEqual(rec.calls[2][1], ["npm", "run", "build"])

    def test_slugs_and_build_skipped_when_install_fails(self):
        rec = Recorder(rc_by_stage={"site-install": 1})
        with mock.patch.object(site_stage, "run_streamed", rec):
            rc = site_stage.run(SimpleNamespace(), _fake_repos(), "2026-08-22", _fake_runlog())
        self.assertEqual(rc, 1)
        self.assertEqual(rec.stages, ["site-install"])  # slugs/build never attempted

    def test_build_skipped_when_slugs_fail(self):
        rec = Recorder(rc_by_stage={"site-slugs": 3})
        with mock.patch.object(site_stage, "run_streamed", rec):
            rc = site_stage.run(SimpleNamespace(), _fake_repos(), "2026-08-22", _fake_runlog())
        self.assertEqual(rc, 3)
        self.assertEqual(rec.stages, ["site-install", "site-slugs"])  # build never attempted


class SiteDeployUnitTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_file = Path(tmp.name) / "data" / "site_deploy_state.json"
        for patcher in (
            mock.patch.object(site_deploy_stage, "FRONTEND",
                              dataclasses.replace(deploy_record.SITE, state_file=self.state_file)),
            mock.patch.object(deploy_record, "download", side_effect=fake_download),
        ):
            self.download = patcher.start()
            self.addCleanup(patcher.stop)

    def _deploy(self, rec, conclusion="success"):
        follow = FakeFollow(conclusion)
        with mock.patch.object(site_deploy_stage, "run_streamed", rec), \
             mock.patch.object(site_deploy_stage, "follow", follow):
            rc = site_deploy_stage.run(SimpleNamespace(), _fake_repos(), "2026-08-22", _fake_runlog())
        return rc, follow

    def test_success_records_the_deployed_run(self):
        rc, _follow = self._deploy(Recorder())
        self.assertEqual(rc, 0)
        self.download.assert_called_once_with("Surxe/WRFrontiersDB-Site", "1")
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["run_id"], "1")
        self.assertEqual(state["data_commit"], "abc1234def")
        self.assertEqual(state["data_version"], "2026-08-22")
        self.assertTrue(state["deployed_at_utc"].endswith("Z"))

    def test_failed_ci_run_is_not_recorded(self):
        self._deploy(Recorder(), conclusion="failure")
        self.assertFalse(self.state_file.exists())

    def test_missing_artifact_fails_the_stage(self):
        self.download.side_effect = deploy_record.DeployRecordError("no artifact")
        rc, _follow = self._deploy(Recorder())
        self.assertEqual(rc, 1)
        self.assertFalse(self.state_file.exists())

    def test_artifact_from_another_run_fails_the_stage(self):
        self.download.side_effect = lambda repo, run_id: fake_download(repo, "2")
        rc, _follow = self._deploy(Recorder())
        self.assertEqual(rc, 1)
        self.assertFalse(self.state_file.exists())

    def test_unwritable_state_fails_the_stage(self):
        self.state_file.parent.parent.joinpath("data").write_text("not a dir")
        rc, _follow = self._deploy(Recorder())
        self.assertEqual(rc, 1)

    def test_dispatch_command_then_follows_ci(self):
        rec = Recorder()
        rc, follow = self._deploy(rec)
        self.assertEqual(rc, 0)
        self.assertEqual(rec.stages, ["site-deploy"])
        self.assertEqual(
            rec.calls[0][1],
            ["gh", "workflow", "run", "ci.yaml", "-R",
             "Surxe/WRFrontiersDB-Site", "--ref", "main"],
        )
        self.assertEqual(follow.calls, [("Surxe/WRFrontiersDB-Site", "ci.yaml")])

    def test_failed_ci_run_fails_the_stage(self):
        rc, _follow = self._deploy(Recorder(), conclusion="failure")
        self.assertEqual(rc, 1)

    def test_failed_dispatch_is_not_followed(self):
        rc, follow = self._deploy(Recorder(rc_by_stage={"site-deploy": 4}))
        self.assertEqual(rc, 4)
        self.assertEqual(follow.calls, [])

    def test_targets_main_ref(self):
        self.assertEqual(site_deploy_stage.SITE_DEPLOY_REF, "main")
        self.assertEqual(site_deploy_stage.SITE_REPO, "Surxe/WRFrontiersDB-Site")


class RunWiringTests(unittest.TestCase):
    def test_patch_day_enables_deploy(self):
        self.assertIn("should_deploy_site", run._PATCH_DAY_FLAGS)

    def test_deploy_runs_after_site_on_happy_path(self):
        rec = self._drive(["--should-build-site", "true", "--should-deploy-site", "true"])
        self.assertEqual(rec.stages, ["site-install", "site-slugs", "site", "site-deploy"])

    def test_build_break_stops_before_deploy(self):
        rec = self._drive(
            ["--should-build-site", "true", "--should-deploy-site", "true"],
            rc_by_stage={"site": 1},
            expect_rc=1,
        )
        self.assertIn("site", rec.stages)
        self.assertNotIn("site-deploy", rec.stages)  # never dispatched on a broken build

    def test_deploy_only_run(self):
        rec = self._drive(["--should-deploy-site", "true"])
        self.assertEqual(rec.stages, ["site-deploy"])

    # -- helper: drive run.main with all subprocess work stubbed ------------
    def _drive(self, extra_argv, *, rc_by_stage=None, expect_rc=0) -> Recorder:
        rec = Recorder(rc_by_stage=rc_by_stage)
        with tempfile.TemporaryDirectory() as tmp:
            # Pin every non-tested stage gate OFF explicitly: leaving them unset
            # lets optionsconfig's "all-unset -> all-on" rule turn on EXPORT/PARSE,
            # which would run the real sub-repos. The stages under test are enabled
            # by extra_argv.
            argv = [
                "--should-export", "false",
                "--should-parse", "false",
                "--should-push-data", "false",
                "--should-build-index", "false",
            ] + extra_argv + [
                "--game-version", "2026-08-22",
                "--assume-manifest-confirmed", "true",
                "--log-dir", tmp,
            ]
            args = _parse(argv)
            with mock.patch.object(run.preflight, "validate", lambda *a, **k: None), \
                 mock.patch.object(run.Repos, "prune_old_versions", lambda *a, **k: None), \
                 mock.patch.object(site_stage, "run_streamed", rec), \
                 mock.patch.object(site_deploy_stage, "run_streamed", rec), \
                 mock.patch.object(site_deploy_stage, "follow", FakeFollow("success")), \
                 mock.patch.object(site_deploy_stage, "FRONTEND", dataclasses.replace(
                     deploy_record.SITE, state_file=Path(tmp) / "deploy_state.json")), \
                 mock.patch.object(deploy_record, "download", side_effect=fake_download):
                rc = run.main(args)
        self.assertEqual(rc, expect_rc)
        return rec


class PreflightDeployGateTests(unittest.TestCase):
    def _opts(self, **over):
        base = dict(
            should_export=False, should_get_mapper=False, should_download_steam_game=False,
            should_parse=False, should_push_data=False, should_build_site=False,
            should_deploy_site=False, should_batch_export=False,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_missing_gh_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(wrf_root=Path(tmp), repos_dir=Path(tmp))
            with mock.patch.object(preflight.shutil, "which", return_value=None):
                with self.assertRaises(preflight.PreflightError) as ctx:
                    preflight.validate(self._opts(should_deploy_site=True), repos,
                                       game_version="2026-08-22")
        self.assertIn("gh", str(ctx.exception))

    def test_present_gh_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(wrf_root=Path(tmp), repos_dir=Path(tmp))
            with mock.patch.object(preflight.shutil, "which", return_value="/usr/bin/gh"):
                preflight.validate(self._opts(should_deploy_site=True), repos,
                                   game_version="2026-08-22")  # no raise


# --- small fakes ---------------------------------------------------------
class FakeFollow:
    """Stand-in for gh_runs.follow_dispatched_run: records the run it was asked to follow."""

    def __init__(self, conclusion: str) -> None:
        self.conclusion = conclusion
        self.calls: list[tuple[str, str]] = []

    def __call__(self, repo, workflow, since, **kwargs):
        self.calls.append((repo, workflow))
        return gh_runs.WorkflowRun(url="https://example.invalid/run/1", run_id="1",
                                   status="completed", conclusion=self.conclusion)


def fake_download(repo: str, run_id: str) -> dict:
    """Stand-in for deploy_record.download: the record a run's artifact holds."""
    return {"app": repo.split("/")[1], "run_id": run_id, "data_commit": "abc1234def",
            "data_version": "2026-08-22", "built_at_utc": "2026-08-22T10:00:00Z"}


def _fake_repos() -> Repos:
    return Repos(wrf_root=Path("/tmp"), repos_dir=Path("/tmp"))


def _fake_runlog():
    return SimpleNamespace(stage_log_path=lambda stage: Path("/tmp") / f"{stage}.log",
                           stage_sink=lambda stage: contextlib.nullcontext(Path("/tmp") / f"{stage}.log"))


if __name__ == "__main__":
    unittest.main()
