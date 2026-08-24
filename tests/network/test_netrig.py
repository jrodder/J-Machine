"""Spec §7 test 0: rig smoke. Real RNS 1.5.0 / LXMF 1.1.1 over loopback
TCP in two OS processes: announce -> recall -> RNS request (page) ->
LXMF delivery -> reply. Proves the transport layer before the protocol
is built on it."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    import RNS  # noqa: F401
    import LXMF  # noqa: F401
    HAVE_RNS = True
except ImportError:
    HAVE_RNS = False


@unittest.skipUnless(HAVE_RNS, "rns/lxmf not installed (pip install rns lxmf)")
class Smoke(unittest.TestCase):
    def test_client_loglevel(self):
        # Task 4 fix round 2 regression guard: the scaffolded config
        # loglevel=-1 is clamped to 0 (LOG_CRITICAL) at parse time
        # (site-packages/RNS/Reticulum.py:468), so silence must come from
        # the runtime override in Client.__init__ (RNS.loglevel =
        # RNS.LOG_NONE). The client stdout is the session transcript
        # (spec §7); one log line would pollute it. port=4243: no host —
        # this test exercises the loglevel, not the transport.
        from jclient.client import Client
        with tempfile.TemporaryDirectory() as d:
            Client(d, str(Path(d) / "identity"), port=4243)
        self.assertEqual(RNS.loglevel, RNS.LOG_NONE)

    def test_smoke(self):
        from tests.network import netrig
        with tempfile.TemporaryDirectory() as d:
            host_d, client_d = Path(d) / "host", Path(d) / "client"
            # minimal inline responder (smoke_host.py) — no jhost dependency;
            # proves the transport before the real host exists (Task 3)
            h = netrig.spawn([sys.executable, "-u", "-m",
                              "tests.network.smoke_host",
                              str(host_d), "4242", "Smoke"],
                             "host", host_d)
            try:
                self.assertTrue(
                    netrig.wait_file(host_d / netrig.DEST_JSON, 120),
                    netrig.logs_tail(host_d))
                rc, out, err = netrig.run_captured(
                    [sys.executable, "-u", "-m", "jclient", "smoke",
                     "--data-dir", str(client_d),
                     "--host-json", str(host_d / netrig.DEST_JSON)],
                    "client", client_d, timeout=180)
                self.assertEqual(rc, 0,
                                 out + "\n== host ==\n" + netrig.logs_tail(host_d))
                self.assertIn("SMOKE-OK", out)
            finally:
                h.terminate()
                h.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
