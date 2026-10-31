#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 server (Lightsail or EC2). Run from the unzipped repo:
#   sudo DOMAIN=errand.example.com ./deploy/setup.sh
# Installs Caddy (HTTPS + password), copies the app to /opt/errand-agent, runs it as a systemd service.
set -euo pipefail
: "${DOMAIN:?Set DOMAIN, e.g. sudo DOMAIN=errand.example.com ./deploy/setup.sh}"
ADMIN_USER="${ADMIN_USER:-demo}"
APP_DIR=/opt/errand-agent
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing Python and Caddy"
apt-get update -y
apt-get install -y python3 debian-keyring debian-archive-keyring apt-transport-https curl gnupg openssl
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'

echo "==> Copying the app to $APP_DIR"
id errand >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin errand
mkdir -p "$APP_DIR" /etc/errand
(cd "$SRC" && tar --exclude=./var --exclude=./.git -cf - .) | (cd "$APP_DIR" && tar -xf -)
mkdir -p "$APP_DIR/var"
chown -R errand:errand "$APP_DIR"

if [ ! -f /etc/errand/errand.env ]; then
  cat > /etc/errand/errand.env <<ENV
# Add your key, then: sudo systemctl restart errand
ANTHROPIC_API_KEY=
ERRAND_MODEL=claude-haiku-5-5
# 1 enables the Faults tab, clock and reset (needed for the demo). Set 0 when you are done recording.
ERRAND_DEMO=1
# false hashes user messages in traces instead of storing them
TRACE_CAPTURE_CONTENT=true
ENV
fi
chmod 600 /etc/errand/errand.env

echo "==> Configuring HTTPS and a password (Caddy)"
PASSWORD="$(openssl rand -base64 15 | tr -d '/+=')"
HASH="$(caddy hash-password --plaintext "$PASSWORD")"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    basicauth {
        $ADMIN_USER $HASH
    }
    reverse_proxy 127.0.0.1:8765 {
        # stream agent steps to the browser as they happen
        flush_interval -1
    }
}
CADDY

echo "==> Starting services"
cp "$SRC/deploy/errand.service" /etc/systemd/system/errand.service
systemctl daemon-reload
systemctl enable --now errand
systemctl restart errand
systemctl reload caddy || systemctl restart caddy

echo
echo "Done. Open https://$DOMAIN"
echo "  user:     $ADMIN_USER"
echo "  password: $PASSWORD   (shown once; rerun this script to set a new one)"
echo "Next: put your key in /etc/errand/errand.env, then: sudo systemctl restart errand"
