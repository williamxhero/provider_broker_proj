#!/usr/bin/env bash
set -euo pipefail

VERSION=${1:?version is required}
STAGE=${2:?stage directory is required}
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]

APP_ROOT=/data/provider-broker
RELEASES="$APP_ROOT/releases"
RELEASE="$RELEASES/$VERSION"
TEMP_RELEASE="$RELEASES/.${VERSION}.tmp-$$"
WHEEL="$STAGE/provider_broker-${VERSION}-py3-none-any.whl"
test -f "$WHEEL"
test ! -e "$RELEASE"

cleanup() { rm -rf -- "$TEMP_RELEASE"; }
trap cleanup EXIT
install -d -o yosef -g yosef "$RELEASES" "$APP_ROOT/data" "$APP_ROOT/secrets"
install -d -o yosef -g yosef "$TEMP_RELEASE"
python3 -m venv "$TEMP_RELEASE/venv"
"$TEMP_RELEASE/venv/bin/pip" install --upgrade pip >/dev/null
"$TEMP_RELEASE/venv/bin/pip" install "$WHEEL" >/dev/null
"$TEMP_RELEASE/venv/bin/python" - <<'PY'
from provider_broker import app, upstream

assert hasattr(upstream, "AttemptAudit")
assert not hasattr(upstream, "StreamingAttempt")
assert "result[\"attempt\"]" not in open(app.__file__, encoding="utf-8").read()
PY
install -m 755 "$STAGE/smoke.py" "$TEMP_RELEASE/smoke.py"
install -m 755 "$STAGE/transport_matrix.py" "$TEMP_RELEASE/transport_matrix.py"
install -m 755 "$STAGE/production_shape_smoke.py" "$TEMP_RELEASE/production_shape_smoke.py"
install -m 755 "$STAGE/firewall.sh" "$TEMP_RELEASE/firewall.sh"
mv -- "$TEMP_RELEASE" "$RELEASE"

ENV_FILE="$APP_ROOT/secrets/broker.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Broker secret file must be provisioned before release installation" >&2
  exit 1
fi
chown yosef:yosef "$ENV_FILE"
chmod 600 "$ENV_FILE"

# CPA is the reference transport used by desktop clients.  Keep its local
# inference credential in the already protected Broker environment so the
# release-contained differential canary can compare both paths without printing
# or copying the key to the workstation.
CPA_CONFIG=/data/cpa/CLIProxyAPI/config.yaml
if [[ -f "$CPA_CONFIG" ]]; then
  cpa_inference_key=$(python3 -c "import yaml; print((yaml.safe_load(open('$CPA_CONFIG', encoding='utf-8')).get('api-keys') or [''])[0])" 2>/dev/null || true)
  if [[ -n "$cpa_inference_key" ]]; then
    tmp_env=$(mktemp "$APP_ROOT/secrets/.broker.env.XXXXXX")
    grep -v '^CPA_INFERENCE_KEY=' "$ENV_FILE" > "$tmp_env" || true
    printf 'CPA_INFERENCE_KEY=%s\n' "$cpa_inference_key" >> "$tmp_env"
    chown yosef:yosef "$tmp_env"
    chmod 600 "$tmp_env"
    mv -f "$tmp_env" "$ENV_FILE"
  fi
fi

previous_target=""
if [[ -L "$APP_ROOT/current" ]]; then
  previous_target=$(readlink -f "$APP_ROOT/current")
elif [[ -d "$APP_ROOT/app" && -d "$APP_ROOT/venv" ]]; then
  legacy="$RELEASES/legacy-pre-${VERSION}"
  if [[ ! -e "$legacy" ]]; then
    install -d -o yosef -g yosef "$legacy"
    cp -a "$APP_ROOT/app" "$legacy/app"
    cp -a "$APP_ROOT/venv" "$legacy/venv"
  fi
  previous_target="$legacy"
fi

install -m 644 "$STAGE/provider-broker.service" /etc/systemd/system/provider-broker.service
install -m 644 "$STAGE/provider-broker-firewall.service" /etc/systemd/system/provider-broker-firewall.service
systemctl daemon-reload
if [[ -n "$previous_target" ]]; then
  ln -sfn "$previous_target" "$APP_ROOT/previous.next"
  mv -Tf "$APP_ROOT/previous.next" "$APP_ROOT/previous"
fi
ln -s "$RELEASE" "$APP_ROOT/current.next"
mv -Tf "$APP_ROOT/current.next" "$APP_ROOT/current"
systemctl enable --now provider-broker-firewall.service >/dev/null
systemctl enable provider-broker.service >/dev/null
systemctl restart provider-broker.service

healthy=false
for _ in {1..30}; do
  if curl --fail --silent --max-time 2 http://192.168.50.2:8817/healthz >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  if [[ -n "$previous_target" ]]; then
    ln -sfn "$previous_target" "$APP_ROOT/current.rollback"
    mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
    systemctl restart provider-broker.service
  fi
  echo "New release failed health check and was rolled back" >&2
  exit 1
fi

if ! "$RELEASE/venv/bin/python" "$RELEASE/production_shape_smoke.py" --runs 1 --intellect smart --token-count 2000 --deadline-ms 180000 --output-token-limit 2000 \
  || ! "$RELEASE/venv/bin/python" "$RELEASE/production_shape_smoke.py" --runs 1 --intellect expert --token-count 2000 --deadline-ms 180000 --output-token-limit 6000; then
  if [[ -n "$previous_target" ]]; then
    ln -sfn "$previous_target" "$APP_ROOT/current.rollback"
    mv -Tf "$APP_ROOT/current.rollback" "$APP_ROOT/current"
    systemctl restart provider-broker.service
  fi
  echo "New release failed smart/expert structured Broker route canary and was rolled back" >&2
  exit 1
fi

if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  ufw allow from 192.168.50.1 to 192.168.50.2 port 8817 proto tcp >/dev/null
  ufw deny to 192.168.50.2 port 8817 proto tcp >/dev/null
fi

trap - EXIT
printf 'release=%s\n' "$RELEASE"
printf 'previous=%s\n' "${previous_target:-none}"
