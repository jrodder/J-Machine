#!/bin/sh
# One question, one line: did a message reach the live host over the mesh and
# come back, asked from a machine that is NOT the server.
#
# Why it exists. The deployed check (jhost.probe, a timer on the server itself)
# shares a LAN and the same hub connections with the thing it checks. On
# 2026-11-23 it reported probe_ok for six straight days while delivered == 0
# for every real player, because the route it exercises -- its own destination,
# a few hops from the host -- stays warm no matter what happens to distant
# players. A check that only repeats what the checked thing already believes is
# not a check. This one is a stranger on the public testnet.
#
# Needs an RNS config with real hub interfaces; jclient scaffolds a
# loopback-only one, which reaches nothing and proves nothing:
#   mkdir -p ~/.jmachine/client/rns && $EDITOR ~/.jmachine/client/rns/config
# The identity is created on first run (it is a private key; keep it out of git).
#
# Verdict and timing land in the journal (journalctl --user -u jmachine-probe),
# so there is no state file to keep. Exit 0 = round trip complete.
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
py=$repo/.venv/bin/python
[ -x "$py" ] || py=python3
data=${JM_DATA:-$HOME/.jmachine/client}
game=${JM_GAME:-<35bcbef411d9984a21a90bb95766d829>}
budget=${JM_BUDGET:-600}

mkdir -p "$data/rns"
if [ ! -f "$data/rns/config" ]; then
    echo "offbox: no RNS config at $data/rns/config (the scaffolded config is" >&2
    echo "        loopback-only and cannot reach the mesh) -- write one first" >&2
    exit 2
fi

cd "$repo"
if "$py" -u -m jclient ping "$game" --data-dir "$data" \
        --identity "$data/identity" --budget "$budget"; then
    echo "offbox $game: OK from $(uname -n)"
else
    echo "offbox $game: FAILED from $(uname -n)"
    exit 1
fi
