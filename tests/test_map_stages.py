"""The map export and map cook wiring: the Exporter and Parser arguments, the maps dir,
its retention, and the preflight check that the Parser has a map export to cook.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import preflight  # noqa: E402
import run  # noqa: E402
from repos import Repos  # noqa: E402
from stages import export as export_stage  # noqa: E402
from stages import parse as parse_stage  # noqa: E402

VERSION = "2026-10-06"


def _options(**over):
    base = dict(
        log_level="INFO", should_download_dependencies=False, should_download_steam_game=False,
        should_get_mapper=False, should_batch_export=False, should_export_textures=False,
        force_download_dependencies=False, batch_export_release="v1.8.0-test.1", force_steam_download=False,
        force_get_mapper=False, force_export=False, should_export_maps=True, force_export_maps=False,
        headless=True, manifest_id="latest", steam_username=None, steam_password=None,
        should_export=False, should_parse=True, should_parse_maps=True, should_push_data=False,
        target_branch="main", gh_data_repo_pat=None, should_build_site=False, should_deploy_site=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _arg(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


class StageCommandTests(unittest.TestCase):
    def setUp(self):
        self.repos = Repos(wrf_root=Path("/wrf"), repos_dir=Path("/repos"))
        self.runlog = SimpleNamespace(stage_log_path=lambda stage: Path(f"/logs/{stage}.log"))

    def _cmd(self, stage, options):
        recorded = {}

        def fake_run_streamed(cmd, **kwargs):
            recorded["cmd"] = cmd
            return 0

        with mock.patch.object(stage, "run_streamed", fake_run_streamed):
            stage.run(options, self.repos, VERSION, self.runlog)
        return recorded["cmd"]

    def test_export_forwards_the_map_export(self):
        cmd = self._cmd(export_stage, _options(force_export_maps=True))
        self.assertEqual(_arg(cmd, "--should-export-maps"), "true")
        self.assertEqual(_arg(cmd, "--force-export-maps"), "true")
        self.assertEqual(_arg(cmd, "--output-maps-dir"), "/wrf/data/maps/2026-10-06")
        self.assertEqual(_arg(cmd, "--batch-export-release"), "v1.8.0-test.1")

    def test_parse_cooks_the_same_map_export(self):
        cmd = self._cmd(parse_stage, _options())
        self.assertEqual(_arg(cmd, "--should-parse-maps"), "true")
        self.assertEqual(_arg(cmd, "--maps-export-dir"), "/wrf/data/maps/2026-10-06")

    def test_patch_day_exports_maps(self):
        self.assertIn("should_export_maps", run._PATCH_DAY_FLAGS)
        self.assertIn("force_export_maps", run._FORCE_FLAGS)


class RetentionTests(unittest.TestCase):
    def test_old_map_exports_are_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(wrf_root=Path(tmp), repos_dir=Path(tmp))
            for version in ("2026-09-15", "2026-09-29", "2026-10-06"):
                (repos.maps_dir_for_version(version) / "maps").mkdir(parents=True)
            repos.prune_old_versions(keep=2)
            self.assertEqual(sorted(p.name for p in (repos.data_root / "maps").iterdir()), ["2026-09-29", "2026-10-06"])


class PreflightMapsTests(unittest.TestCase):
    def _validate(self, tmp: str, **over):
        repos = Repos(wrf_root=Path(tmp), repos_dir=Path(tmp))
        repos.exports_dir_for_version(VERSION).mkdir(parents=True)
        (repos.parser_dir / ".venv" / "bin").mkdir(parents=True)
        (repos.parser_dir / ".venv" / "bin" / "python").touch()
        preflight.validate(_options(**over), repos, game_version=VERSION)
        return repos

    def test_parse_without_a_map_export_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(preflight.PreflightError) as ctx:
                self._validate(tmp)
        self.assertIn("Map export missing", str(ctx.exception))

    def test_parse_with_a_map_export_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Repos(wrf_root=Path(tmp), repos_dir=Path(tmp)).maps_dir_for_version(VERSION) / "maps").mkdir(parents=True)
            self._validate(tmp)  # no raise

    def test_parse_without_maps_needs_no_map_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._validate(tmp, should_parse_maps=False)  # no raise


if __name__ == "__main__":
    unittest.main()
