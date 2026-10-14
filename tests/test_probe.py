"""src/probe.py's PICS query: every attempt is bounded, so a Steam outage is a
logged ProbeError rather than a silent hang killed by systemd's timeout.

No network: steam.client.SteamClient is faked.

Run: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import gevent
from steam.enums import EResult

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

import probe  # noqa: E402
from probe import ProbeError  # noqa: E402

GID = "1234567890"
PRODUCT_INFO = {
    "apps": {
        probe.APP_ID: {
            "depots": {
                probe.DEPOT_ID: {"manifests": {"public": {"gid": GID}}},
                "branches": {"public": {"buildid": "42", "timeupdated": "1700000000"}},
            }
        }
    }
}


class FakeClient:
    """Stands in for SteamClient; `connect_behaviour` picks the failure mode."""

    instances: list["FakeClient"] = []
    connect_behaviour = "ok"

    def __init__(self):
        self.connect_kwargs = None
        self.disconnected = False
        FakeClient.instances.append(self)

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_behaviour == "hang":
            # What steam's connect(retry=0) does during a CM/directory outage.
            gevent.sleep(3600)
        return self.connect_behaviour != "refuse"

    def anonymous_login(self):
        return EResult.OK

    def get_product_info(self, apps):
        return PRODUCT_INFO

    def logout(self):
        pass

    def disconnect(self):
        self.disconnected = True


class QueryPicsTest(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        patcher = mock.patch("steam.client.SteamClient", FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_query(self, behaviour, retries=2):
        FakeClient.connect_behaviour = behaviour
        return probe.query_pics(retries=retries, delay=0, attempt_timeout=0.2)

    def test_ok_returns_gid_and_connects_with_bounded_retry(self):
        result = self.run_query("ok")
        self.assertEqual(result, {"gid": GID, "buildid": "42", "timeupdated": "1700000000"})
        self.assertEqual(FakeClient.instances[0].connect_kwargs,
                         {"retry": probe.CONNECT_RETRIES})

    def test_hanging_connect_times_out_each_attempt(self):
        start = time.monotonic()
        with self.assertRaisesRegex(ProbeError, "timed out after 0.2s"):
            self.run_query("hang", retries=2)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(len(FakeClient.instances), 2)
        self.assertTrue(all(c.disconnected for c in FakeClient.instances))

    def test_refused_connect_is_a_probe_error(self):
        with self.assertRaisesRegex(ProbeError, "could not connect to any Steam CM"):
            self.run_query("refuse")


if __name__ == "__main__":
    unittest.main()
