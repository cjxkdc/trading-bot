#!/usr/bin/env bash
# Publish the configured owner-authenticated Portal. Run as the instance owner.
set -euo pipefail
umask 077
if [[ "$EUID" -eq 0 || "$#" -lt 1 || "$#" -gt 2 ]]; then
  echo "Usage as instance owner: bash deploy/server/install_web.sh https://STATIC_IP [--agree-tos]" >&2
  exit 1
fi
origin="$1"
if [[ "${2:-}" != '--agree-tos' ]]; then
  echo "Issuing a certificate requires agreement to the Let's Encrypt subscriber terms."
  echo "Review https://letsencrypt.org/repository/ and rerun with --agree-tos."
  exit 1
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd -- "$project_dir"
generated="$(mktemp -d)"
trap 'rm -rf -- "$generated"' EXIT
host="$(python3 deploy/server/web_front.py --origin "$origin" --output "$generated")"
# Stop before root changes if authentication is absent or uses another origin.
.venv/bin/python -c 'import sys; from portal.access import RemoteSettings; s=RemoteSettings.load("."); assert s.origin == sys.argv[1].rstrip("/"), "Configure the same HTTPS origin first"' "$origin"
sudo apt-get update -qq
sudo apt-get install -y nginx python3-venv
if [[ ! -x /opt/trading-bot-certbot/bin/certbot ]]; then
  sudo python3 -m venv /opt/trading-bot-certbot
fi
sudo /opt/trading-bot-certbot/bin/pip install 'certbot>=5.4,<6'
sudo install -d -m 755 /var/lib/trading-bot-web/acme
managed=/etc/nginx/sites-available/trading-bot
enabled=/etc/nginx/sites-enabled/trading-bot
if [[ -e "$managed" ]] && ! sudo head -1 "$managed" | grep -qx '# Managed by Trading Bot HTTPS deployment.'; then
  echo "Existing Nginx site is not managed by this installer; stopped." >&2
  exit 1
fi
backup="/var/backups/trading-bot-web/$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 700 "$backup"
if [[ -e "$managed" ]]; then sudo cp -p "$managed" "$backup/nginx.conf"; fi
# Remove only Ubuntu's packaged default symlink, preserving its source.
if [[ -L /etc/nginx/sites-enabled/default ]] && [[ "$(readlink /etc/nginx/sites-enabled/default)" == /etc/nginx/sites-available/default ]]; then
  sudo mv /etc/nginx/sites-enabled/default "$backup/default-link"
fi
restore_site() {
  if sudo test -f "$backup/nginx.conf"; then
    sudo cp -p "$backup/nginx.conf" "$managed"
  else
    sudo rm -f "$enabled" "$managed"
  fi
  if sudo test -L "$backup/default-link"; then sudo mv "$backup/default-link" /etc/nginx/sites-enabled/default; fi
  sudo nginx -t && sudo systemctl reload nginx
}
trap 'restore_site; rm -rf -- "$generated"' ERR
sudo install -m 644 "$generated/nginx.conf" "$managed"
sudo ln -sfn "$managed" "$enabled"
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
cert_args=(certonly --non-interactive --agree-tos --register-unsafely-without-email --webroot --webroot-path /var/lib/trading-bot-web/acme --cert-name "$host")
if python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$host" 2>/dev/null; then
  cert_args+=(--ip-address "$host" --preferred-profile shortlived)
else
  cert_args+=(-d "$host")
fi
sudo /opt/trading-bot-certbot/bin/certbot "${cert_args[@]}"
python3 deploy/server/web_front.py --origin "$origin" --output "$generated" --tls >/dev/null
sudo install -m 644 "$generated/nginx.conf" "$managed"
sudo nginx -t
sudo install -m 755 "$generated/reload.sh" /usr/local/sbin/trading-bot-web-reload
sudo install -m 644 "$generated/renew.service" /etc/systemd/system/trading-bot-cert-renew.service
sudo install -m 644 "$generated/renew.timer" /etc/systemd/system/trading-bot-cert-renew.timer
sudo systemctl daemon-reload
sudo systemctl enable --now trading-bot-cert-renew.timer
sudo systemctl reload nginx
echo "HTTPS ready: $origin — owner login remains required."
