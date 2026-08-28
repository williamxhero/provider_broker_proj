#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/data/provider-broker
APP_USER=${SUDO_USER:-$USER}
install -d -o "$APP_USER" -g "$APP_USER" "$APP_ROOT"/{app,data,secrets}
rsync -a --delete --exclude .git --exclude .venv ./ "$APP_ROOT/app/"
python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install --upgrade pip >/dev/null
"$APP_ROOT/venv/bin/pip" install "$APP_ROOT/app" >/dev/null

ENV_FILE="$APP_ROOT/secrets/broker.env"
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cpa_pid=$(pgrep -f '[c]pa-manager-plus' | head -n1)
  cpa_key=$(tr '\0' '\n' < "/proc/$cpa_pid/environ" | sed -n 's/^CPA_MANAGEMENT_KEY=//p' | head -n1)
  test -n "$cpa_key"
  cat > "$ENV_FILE" <<EOF
BROKER_DB_PATH=$APP_ROOT/data/broker.sqlite3
BROKER_CLIENT_TOKEN=$(openssl rand -hex 32)
BROKER_ADMIN_TOKEN=$(openssl rand -hex 32)
BROKER_SESSION_SECRET=$(openssl rand -hex 32)
BROKER_ENCRYPTION_KEY=$(openssl rand -base64 32 | tr -d '\n')
CPA_URL=http://127.0.0.1:8317
CPA_MANAGEMENT_KEY=$cpa_key
EOF
  chown "$APP_USER:$APP_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"
fi
install -m 644 "$APP_ROOT/app/deploy/provider-broker.service" /etc/systemd/system/provider-broker.service
systemctl daemon-reload
systemctl enable --now provider-broker.service
if command -v ufw >/dev/null; then
  ufw allow from 192.168.50.1 to any port 8817 proto tcp
  ufw deny 8817/tcp
fi
curl --fail --silent http://192.168.50.2:8817/healthz >/dev/null
