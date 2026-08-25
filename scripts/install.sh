#!/usr/bin/env bash
# Bootstrap + systemd install for the J-Machine Reticulum game host.
# Idempotent — safe to re-run. Run from anywhere; the repo root is resolved
# from this script's own path.
#
#   ./scripts/install.sh
#
# Requires: python3 >= 3.10, python3-venv, sudo access.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
DATA_DIR="$REPO_ROOT/data"
RNS_CFG="$DATA_DIR/rns/config"
TEMPLATE="$REPO_ROOT/deploy/rns-config.template"
UNIT_PATH="/etc/systemd/system/jhost.service"
DEST="$DATA_DIR/host-destinations.json"

# user the service runs as (the invoking user)
RUN_USER="${SUDO_USER:-$(id -un)}"

# run a command as root: direct if already root, else via sudo
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
    echo "error: $REPO_ROOT/pyproject.toml not found — is this a J-Machine checkout?" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "error: python3 >= 3.10 required" >&2; exit 1; }

# --- 1. venv + deps
if [ ! -x "$VENV/bin/python" ]; then
    echo "== creating venv at $VENV"
    python3 -m venv "$VENV" \
        || { echo "error: 'python3 -m venv' failed — install python3-venv" >&2; exit 1; }
fi
echo "== installing dependencies"
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install "rns>=1.5,<1.6" "lxmf>=1.1,<1.2"

# --- 2. RNS config (from template, non-clobbering)
mkdir -p "$DATA_DIR/rns"
if [ -f "$RNS_CFG" ]; then
    echo "== keeping existing $RNS_CFG"
else
    echo "== writing $RNS_CFG from template"
    cp "$TEMPLATE" "$RNS_CFG"
fi

# --- 3. systemd unit
echo "== writing $UNIT_PATH"
as_root tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=J-Machine Reticulum game host
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO_ROOT
ExecStart=$VENV/bin/python -m jhost games/ --data-dir data/
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- 4. enable + start
echo "== enabling and starting jhost.service"
as_root systemctl daemon-reload
as_root systemctl enable jhost.service >/dev/null
as_root systemctl restart jhost.service

# --- 5. wait for the host to come up (writes data/host-destinations.json)
echo "== waiting for the host to come up (up to 60s)..."
for _ in $(seq 1 60); do
    [ -f "$DEST" ] && break
    if ! as_root systemctl is-active --quiet jhost.service; then
        echo "error: jhost.service is not active — journal tail:" >&2
        as_root journalctl -u jhost.service -n 30 --no-pager >&2 || true
        exit 1
    fi
    sleep 1
done

if [ ! -f "$DEST" ]; then
    echo "error: host did not produce $DEST within 60s — journal tail:" >&2
    as_root journalctl -u jhost.service -n 50 --no-pager >&2 || true
    exit 1
fi

echo "== host is up — addresses:"
cat "$DEST"

echo
echo "Next steps:"
echo "  1. (optional) edit $RNS_CFG to toggle testnet hubs, then:"
echo "     sudo systemctl restart jhost"
echo "  2. Share the page hash above out-of-band so NomadNet users can browse it."
echo "  3. Logs: journalctl -u jhost -f"
echo
echo "Note: these are fresh addresses (identities are generated here). To keep"
echo "the old node's addresses and player save slots, copy its data/ over before"
echo "the first start instead."
