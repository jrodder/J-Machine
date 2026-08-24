"""Test-only RNS rig (spec §7): two OS processes, real RNS instances,
loopback TCP pairing, temp data dirs. RNS is a process singleton (spec §2),
so host and client are subprocesses. Host responders = `python -m
tests.network.smoke_host` (Task 2 smoke) or `python -m jhost` (Task 5
network suite); clients = `python -m jclient`. Hash handoff via
data/host-destinations.json (also the operator feature, spec §3).
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jhost.protocol import DEST_JSON, unpretty  # re-exports (stdlib-only)


# ---------------------------------------------------------------- rig
def spawn(argv, name, out_dir):
    """Host-style subprocess: stdout+stderr to out_dir/proc-<name>.log."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fh = open(out_dir / f"proc-{name}.log", "wb")
    return subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT)


def run_captured(argv, name, out_dir, timeout=180):
    """Run argv to completion; tee output to out_dir/proc-<name>.log.
    Returns (rc, stdout_text, stdout_text) — client CLI uses one stream."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(out_dir / f"proc-{name}.log", "wb")
    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        raise AssertionError(
            f"{name} timed out:\n{logs_tail(out_dir, 60)}") from None
    log.write(out)
    log.close()
    text = out.decode(errors="replace")
    return p.returncode, text, text


def wait_file(path, timeout=120, interval=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(interval)
    return False


def logs_tail(d, n=40):
    """Last n lines of every *.log under d (RNS failures are opaque
    without them, spec §7)."""
    out = []
    for p in sorted(Path(d).rglob("*.log")):
        out.append(f"== {p} ==\n"
                   + "\n".join(p.read_text(errors="replace").splitlines()[-n:]))
    return "\n".join(out) if out else "(no logs)"


# -------------------------------------------------------- jclient driver
def _client_argv(game_addr, work_dir, port=4242):
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    return [sys.executable, "-u", "-m", "jclient", "play", game_addr,
            "--identity", str(work_dir / "identity"),
            "--data-dir", str(work_dir), "--port", str(port)]


def play_once(game_addr, lines, work_dir, name, timeout=180, port=4242):
    """jclient play, all lines on stdin at once. Returns (rc, stdout).
    The client prints each reply's text verbatim (plus its own markers),
    so the tests compare `norm(stdout)` — see tests/network/test_network.py."""
    return _play_once_impl(_client_argv(game_addr, work_dir, port), name,
                           work_dir,
                           "".join(l + "\n" for l in lines).encode(), timeout)


def _play_once_impl(argv, name, work_dir, stdin_bytes, timeout):
    out_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(out_dir / f"proc-{name}.log", "wb")
    p = subprocess.Popen(argv, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = p.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
        raise AssertionError(
            f"{name} timed out:\n{logs_tail(out_dir, 60)}") from None
    log.write(out)
    log.close()
    return p.returncode, out.decode(errors="replace")


def play_proc(game_addr, work_dir, name, port=4242):
    """jclient play with live stdin pipes (two-players interleaving).
    stdout is line-streamed (python -u) so reply blocks arrive as printed."""
    out_dir = Path(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(_client_argv(game_addr, work_dir, port),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
