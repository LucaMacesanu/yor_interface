#!/bin/bash
# Install yor_hub as a boot service on this machine.
#   scripts/install.sh            # uses config/pi.yaml
#   scripts/install.sh orin.yaml  # on the Orin
set -euo pipefail
DIR=$(cd "$(dirname "$0")/.." && pwd)
CONFIG=${1:-pi.yaml}
[ -f "$DIR/config/$CONFIG" ] || { echo "no config/$CONFIG"; exit 1; }
command -v tmux >/dev/null || { echo "install tmux first"; exit 1; }

if [ ! -x "$DIR/.venv/bin/python" ]; then
    python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

sed -e "s#@USER@#$USER#g" -e "s#@HOME@#$HOME#g" -e "s#@DIR@#$DIR#g" -e "s#@CONFIG@#$CONFIG#g" \
    "$DIR/systemd/yor-hub.service" | sudo tee /etc/systemd/system/yor-hub.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable yor-hub.service
sudo systemctl restart yor-hub.service
sleep 3
systemctl --no-pager --lines=5 status yor-hub.service || true

PORT=$(grep -m1 -E '^\s*port:' "$DIR/config/$CONFIG" | awk '{print $2}')
TOKEN=$(cat "$HOME/.config/yor_hub/token" 2>/dev/null || echo "<generated on first start>")
echo
echo "Open: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/?token=$TOKEN"
