"""The INDEX stage: what fails it, what only warns, and when index/ is pushed.

The data repo's tools are faked (their own tests live in WRFrontiersDB-Data), and
git is never run: data_repo.publish is stubbed with a recorder.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
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

from loguru import logger  # noqa: E402

import data_repo  # noqa: E402
from repos import Repos  # noqa: E402
from stages import index as index_stage  # noqa: E402


class FakeDataRepoError(RuntimeError):
    pass


def _fake_tools(*, slugs=None, nicks=None, aliases=None, abbrevs=None, codes=None, releases_error=False,
                new_bots=()):
    slugs = slugs or SimpleNamespace(slug_map={"a": "a"}, skipped=[], collisions=[], changed=True)
    nicks = nicks or SimpleNamespace(nicknames={}, conflicts=[], ambiguous=[], changed=False)
    aliases = aliases or SimpleNamespace(aliases={}, changed=False)
    abbrevs = abbrevs or SimpleNamespace(abbreviations={}, unused=[], changed=False)
    codes = codes or SimpleNamespace(registry={"modules": {}}, appended=[], errors=[], changed=False)

    def write_slug_map(data_dir):
        if isinstance(slugs, Exception):
            raise slugs
        return slugs

    def write_nicknames(data_dir):
        if isinstance(nicks, Exception):
            raise nicks
        return nicks

    def write_aliases(data_dir):
        if isinstance(aliases, Exception):
            raise aliases
        return aliases

    def write_build_codes(data_dir):
        if isinstance(codes, Exception):
            raise codes
        return codes

    def write_abbreviations(data_dir):
        if isinstance(abbrevs, Exception):
            raise abbrevs
        return abbrevs

    def record_releases(data_dir, version, manifest_id):
        if releases_error:
            raise FakeDataRepoError("roster unreadable")
        return SimpleNamespace(new_bots=list(new_bots), backfilled=[], orphaned_refs=[],
                               changed=bool(new_bots), suspected_rename=False)

    def record_patch_manifests(data_dir, builds):
        return SimpleNamespace(added=[], filled=[], conflicts=[], changed=False)

    return SimpleNamespace(
        paths=SimpleNamespace(DataRepoError=FakeDataRepoError, INDEX_REL=Path("index")),
        slug_map=SimpleNamespace(write_slug_map=write_slug_map),
        nicknames=SimpleNamespace(write_nicknames=write_nicknames),
        robot_parts=SimpleNamespace(write_aliases=write_aliases),
        abbreviations=SimpleNamespace(write_abbreviations=write_abbreviations),
        build_codes=SimpleNamespace(write_build_codes=write_build_codes),
        releases=SimpleNamespace(record_releases=record_releases,
                                 record_patch_manifests=record_patch_manifests),
    )


class IndexStageTests(unittest.TestCase):
    def _run(self, tools, *, push=True, publish_error=None):
        published = []

        def publish(data_dir, paths, *, pat, branch, message):
            if publish_error:
                raise publish_error
            published.append((paths, branch, message))
            return True

        options = SimpleNamespace(should_push_data=push, gh_data_repo_pat="pat",
                                  target_branch="main")
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(wrf_root=Path(tmp), repos_dir=Path(tmp))
            runlog = SimpleNamespace(stage_sink=lambda stage: contextlib.nullcontext())
            with mock.patch.object(data_repo, "import_tools", lambda d: tools), \
                 mock.patch.object(data_repo, "publish", publish), \
                 mock.patch.object(index_stage, "_collect_builds", lambda m, v: {}):
                rc = index_stage.run(options, repos, "2026-10-06", runlog)
        return rc, published

    def test_changed_slug_map_is_pushed(self):
        rc, published = self._run(_fake_tools())
        self.assertEqual(rc, 0)
        self.assertEqual(len(published), 1)
        paths, branch, message = published[0]
        self.assertEqual((paths, branch), ([Path("index")], "main"))
        self.assertIn("update slug map", message)

    def test_nothing_changed_pushes_nothing(self):
        unchanged = SimpleNamespace(slug_map={}, skipped=[], collisions=[], changed=False)
        rc, published = self._run(_fake_tools(slugs=unchanged))
        self.assertEqual((rc, published), (0, []))

    def test_collision_fails_before_pushing(self):
        clash = SimpleNamespace(slug_map={}, skipped=[], changed=True,
                                collisions=[("Pilot", "same", ["a", "b"])])
        rc, published = self._run(_fake_tools(slugs=clash))
        self.assertEqual((rc, published), (1, []))

    def test_unreadable_data_fails(self):
        rc, published = self._run(_fake_tools(slugs=FakeDataRepoError("no Pilot.json")))
        self.assertEqual((rc, published), (1, []))

    def test_skipped_objects_only_warn(self):
        skipped = SimpleNamespace(slug_map={}, skipped=[("p1", "Pilot: empty slug")],
                                  collisions=[], changed=True)
        rc, published = self._run(_fake_tools(slugs=skipped))
        self.assertEqual((rc, len(published)), (0, 1))

    def test_releases_failure_is_advisory(self):
        rc, published = self._run(_fake_tools(releases_error=True))
        self.assertEqual((rc, len(published)), (0, 1))  # the slug map still goes out

    def test_changed_nicknames_are_pushed(self):
        unchanged = SimpleNamespace(slug_map={}, skipped=[], collisions=[], changed=False)
        nicks = SimpleNamespace(nicknames={"p": ["Marcus"]}, conflicts=[], ambiguous=[], changed=True)
        rc, published = self._run(_fake_tools(slugs=unchanged, nicks=nicks))
        self.assertEqual((rc, len(published)), (0, 1))
        self.assertIn("update nicknames", published[0][2])

    def test_nickname_conflict_is_advisory(self):
        nicks = SimpleNamespace(nicknames={}, conflicts=[("marcus", ["a", "b"])], ambiguous=[],
                                changed=True)
        with self._loguru_errors() as errors:
            rc, published = self._run(_fake_tools(nicks=nicks))
        self.assertEqual((rc, len(published)), (0, 1))  # the slug map still goes out
        self.assertTrue(any("nickname conflict" in e for e in errors))

    def test_nicknames_failure_is_advisory(self):
        rc, published = self._run(_fake_tools(nicks=FakeDataRepoError("no Pilot.json")))
        self.assertEqual((rc, len(published)), (0, 1))

    def test_changed_aliases_and_abbreviations_are_pushed(self):
        unchanged = SimpleNamespace(slug_map={}, skipped=[], collisions=[], changed=False)
        aliases = SimpleNamespace(aliases={"m": ["Wyrm Chassis"]}, changed=True)
        abbrevs = SimpleNamespace(abbreviations={"r": "relic"}, unused=[], changed=True)
        rc, published = self._run(_fake_tools(slugs=unchanged, aliases=aliases, abbrevs=abbrevs))
        self.assertEqual((rc, len(published)), (0, 1))
        self.assertIn("update aliases, update abbreviations", published[0][2])

    def test_aliases_and_abbreviations_failures_are_advisory(self):
        broken = FakeDataRepoError("no Module.json")
        rc, published = self._run(_fake_tools(aliases=broken, abbrevs=broken))
        self.assertEqual((rc, len(published)), (0, 1))  # the slug map still goes out

    @contextlib.contextmanager
    def _loguru_errors(self):
        """Collect the stage's ERROR messages (it logs with loguru, not logging)."""
        errors: list[str] = []
        sink = logger.add(lambda m: errors.append(m.record["message"]), level="ERROR")
        try:
            yield errors
        finally:
            logger.remove(sink)

    def test_new_robot_named_in_commit(self):
        bot = {"id": "wyrm", "name": "Wyrm", "character_type": "Mech"}
        _rc, published = self._run(_fake_tools(new_bots=[bot]))
        self.assertIn("add 1 newly-released robot(s)", published[0][2])

    def test_push_off_keeps_changes_local(self):
        rc, published = self._run(_fake_tools(), push=False)
        self.assertEqual((rc, published), (0, []))

    def test_changed_build_codes_are_pushed(self):
        unchanged = SimpleNamespace(slug_map={}, skipped=[], collisions=[], changed=False)
        codes = SimpleNamespace(registry={"modules": {"m": {}}}, appended=["module m"], errors=[],
                                changed=True)
        rc, published = self._run(_fake_tools(slugs=unchanged, codes=codes))
        self.assertEqual((rc, len(published)), (0, 1))
        self.assertIn("update build codes", published[0][2])

    def test_build_code_break_fails_before_pushing(self):
        codes = SimpleNamespace(registry={"modules": {}}, appended=[], changed=False,
                                errors=["DA_Module_TorsoAres.1 lost its Ability socket"])
        rc, published = self._run(_fake_tools(codes=codes))
        self.assertEqual((rc, published), (1, []))  # the changed slug map isn't pushed either

    def test_unreadable_build_codes_fail(self):
        rc, published = self._run(_fake_tools(codes=FakeDataRepoError("bad registry")))
        self.assertEqual((rc, published), (1, []))

    def test_failed_push_fails(self):
        rc, _published = self._run(_fake_tools(),
                                   publish_error=data_repo.DataRepoGitError("rejected"))
        self.assertEqual(rc, 1)

    def test_missing_tools_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(wrf_root=Path(tmp), repos_dir=Path(tmp))
            runlog = SimpleNamespace(stage_sink=lambda stage: contextlib.nullcontext())
            options = SimpleNamespace(should_push_data=True, gh_data_repo_pat="pat",
                                      target_branch="main")
            with mock.patch.dict(sys.modules, {"wrfdb_data": None}):
                rc = index_stage.run(options, repos, "2026-10-06", runlog)
        self.assertEqual(rc, 1)


class ImportToolsTests(unittest.TestCase):
    def test_imports_from_the_data_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "tools" / "wrfdb_data"
            package.mkdir(parents=True)
            for name in ("__init__", "abbreviations", "build_codes", "nicknames", "paths", "releases",
                         "robot_parts"):
                (package / f"{name}.py").write_text("", encoding="utf-8")
            (package / "slug_map.py").write_text("def write_slug_map(d):\n    return 'built'\n",
                                                  encoding="utf-8")
            with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)):
                for name in [m for m in sys.modules if m.startswith("wrfdb_data")]:
                    del sys.modules[name]
                tools = data_repo.import_tools(Path(tmp))
                self.assertEqual(tools.slug_map.write_slug_map(None), "built")

if __name__ == "__main__":
    unittest.main()
