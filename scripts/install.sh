#!/bin/bash
# Install yor_hub as a boot service on this machine.
#   scripts/install.sh            # uses config/pi.yaml
#   scripts/install.sh orin.yaml  # on the Orin
set -euo pipefail
DIR=$(cd "$(dirname "$0")/.." && pwd)
CONFIG=${1:-pi.yaml}
[ -f "$DIR/config/$CONFIG" ] || { echo "no config/$CONFIG"; exit 1; }
command -v tmux >/dev/null || { echo "install tmux first"; exit 1; }

if [ ! -x "$DIR/.venv/bin/pip" ]; then
    if ! python3 -m venv "$DIR/.venv" >/dev/null 2>&1; then
        # No python3-venv/ensurepip (the Orin): make the venv without pip, then
        # bootstrap pip into it from the system pip. --ignore-installed because the
        # system pip is on sys.path, so a plain install would be a no-op.
        echo "python3 -m venv failed (no ensurepip); bootstrapping pip from the system pip"
        rm -rf "$DIR/.venv"
        python3 -m venv --without-pip "$DIR/.venv"
        "$DIR/.venv/bin/python" -c "import sys; sys.path.append('/usr/lib/python3/dist-packages'); \
from pip._internal.cli.main import main; sys.exit(main(['install', '-q', '--ignore-installed', 'pip']))"
    fi
fi
"$DIR/.venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"

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
