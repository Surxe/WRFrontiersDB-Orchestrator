"""The data repo PAT reaches git through the environment only, never the remote URL.

A PAT embedded in origin's URL is written to the data clone's .git/config and stays
on disk between runs. Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(SRC_DIR))

import data_repo  # noqa: E402

FAKE_PAT = "github_pat_FAKE0000000000000000"
PLAIN_URL = "https://github.com/Surxe/WRFrontiersDB-Data.git"


class TestGitAuth(unittest.TestCase):
    def test_git_auth_env_sets_extraheader(self):
        env = data_repo._git_auth_env(FAKE_PAT)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        encoded = env["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
        self.assertEqual(base64.b64decode(encoded).decode(), f"x-access-token:{FAKE_PAT}")

    def test_publish_never_puts_pat_in_args(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs["env"]))
            return subprocess.CompletedProcess(cmd, 0, stdout=" M index\n", stderr="")

        with mock.patch.object(data_repo.subprocess, "run", side_effect=fake_run):
            pushed = data_repo.publish(Path("/tmp/data"), [Path("index")], pat=FAKE_PAT,
                                       branch="main", message="m")

        self.assertTrue(pushed)

        for cmd, _env in calls:
            self.assertNotIn(FAKE_PAT, " ".join(cmd))
        set_url = [cmd for cmd, _ in calls if cmd[1:3] == ["remote", "set-url"]]
        self.assertEqual(set_url, [["git", "remote", "set-url", "origin", PLAIN_URL]])
        push_env = next(env for cmd, env in calls if cmd[1] == "push")
        self.assertIn("GIT_CONFIG_VALUE_0", push_env)

    def test_git_error_redacts_encoded_pat(self):
        encoded = data_repo._basic_auth_value(FAKE_PAT)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=f"bad {FAKE_PAT} {encoded}")
        with mock.patch.object(data_repo.subprocess, "run", return_value=failed):
            with self.assertRaises(data_repo.DataRepoGitError) as ctx:
                data_repo._git(["push"], Path("/tmp"), pat=FAKE_PAT)
        self.assertNotIn(FAKE_PAT, str(ctx.exception))
        self.assertNotIn(encoded, str(ctx.exception))

    def test_real_git_sees_header_and_config_stays_clean(self):
        with tempfile.TemporaryDirectory() as repo_dir:
            subprocess.run(["git", "init", "-q", repo_dir], check=True)
            proc = data_repo._git(
                ["config", "--get", "http.https://github.com/.extraheader"], Path(repo_dir), pat=FAKE_PAT
            )
            self.assertTrue(proc.stdout.startswith("AUTHORIZATION: basic "))
            config = (Path(repo_dir) / ".git" / "config").read_text(encoding="utf-8")
            self.assertNotIn(FAKE_PAT, config)
            self.assertNotIn("extraheader", config)


if __name__ == "__main__":
    unittest.main()
