#!/usr/bin/env bash
# Run as the instance owner, from the extracted release. No AWS resources are created.
set -euo pipefail
umask 077
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [[ "$EUID" -eq 0 ]]; then
  echo "Run as the Ubuntu instance owner, not root." >&2
  exit 1
fi
if [[ "$project_dir" == *[[:space:]]* || "$project_dir" == *%* || "$project_dir" == *\\* ]]; then
  echo "Use a path without spaces, percent signs or backslashes, such as ~/trading-bot." >&2
  exit 1
fi
if [[ "$(uname -m)" != x86_64 ]]; then
  echo "This OpenD deployment target is x86_64. Use an x86_64 instance." >&2
  exit 1
fi
if systemctl --user is-active --quiet trading-bot.service; then
  echo "Stop the existing service gracefully before installing dependencies." >&2
  exit 1
fi
sudo apt-get update
sudo apt-get install -y python3 python3-venv unzip
cd -- "$project_dir"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/server.txt
mkdir -p runs secrets "$HOME/.config/systemd/user"
chmod 700 runs secrets
if [[ ! -f secrets/portal/server.json ]]; then
  echo "Enter your public HTTPS address, using a static IP or domain."
  read -r -p "Portal origin (https://YOUR_STATIC_IP): " portal_origin
  .venv/bin/python -m app.server_setup --origin "$portal_origin"
fi
python3 deploy/server/render_service.py "$project_dir" "$HOME/.config/systemd/user/trading-bot.service"
systemctl --user daemon-reload
systemctl --user enable --now trading-bot.service
sudo loginctl enable-linger "$(id -un)"
echo "Portal installed on private port 5329. Startup does not connect a broker or submit orders."
echo "Publish HTTPS with deploy/server/install_web.sh, then open the address in any browser."
