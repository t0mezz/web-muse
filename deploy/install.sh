#!/bin/sh
# Install web-muse as a per-user systemd service.
# Run from a login shell (not sudo): ./deploy/install.sh
set -eu

SRC="$(CDPATH= cd -- "$(dirname -- "$0")" >/dev/null 2>&1 && pwd)/web-muse.service"
DST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/web-muse.service"

mkdir -p "$(dirname "$DST")"
cp "$SRC" "$DST"
systemctl --user daemon-reload
systemctl --user enable --now web-muse
echo "web-muse enabled. Status: systemctl --user status web-muse"
echo "Logs: journalctl --user -u web-muse -f"
echo "Note: run 'loginctl enable-linger' (needs admin once) to keep it running after logout."
