#!/bin/bash
set -euo pipefail

REPO="/app/DeepFaceLive"
cd "$REPO"

TURN_USER="${DFL_TURN_USER:-dfl}"
TURN_PASS="${DFL_TURN_PASSWORD:-dflturn}"
TURN_REALM="${DFL_TURN_REALM:-deepfacelive}"
CERT_DIR="/etc/coturn"
mkdir -p "$CERT_DIR"

if [[ ! -f "${CERT_DIR}/tls.crt" ]]; then
  openssl req -x509 -newkey rsa:2048 \
    -keyout "${CERT_DIR}/tls.key" \
    -out "${CERT_DIR}/tls.crt" \
    -days 365 -nodes \
    -subj "/CN=${TURN_REALM}"
fi

EXT_IP="${DFL_TURN_EXTERNAL_IP:-}"
if [[ -z "$EXT_IP" ]]; then
  EXT_IP="$(curl -sf --max-time 3 https://api.ipify.org 2>/dev/null || true)"
fi
if [[ -z "$EXT_IP" ]]; then
  EXT_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi

cat > /etc/turnserver.conf <<EOF
listening-port=3478
tls-listening-port=443
listening-ip=0.0.0.0
relay-ip=0.0.0.0
external-ip=${EXT_IP}
realm=${TURN_REALM}
server-name=${TURN_REALM}
fingerprint
lt-cred-mech
user=${TURN_USER}:${TURN_PASS}
cert=${CERT_DIR}/tls.crt
pkey=${CERT_DIR}/tls.key
no-multicast-peers
no-cli
EOF

echo "Starting coturn (TURNS on TCP 443, external-ip=${EXT_IP})"
turnserver -c /etc/turnserver.conf &
sleep 1

TURN_HOST="${DFL_TURN_PUBLIC_HOST:-}"
if [[ -z "$TURN_HOST" && -n "${DFL_STREAM_PUBLISH_URL:-}" ]]; then
  TURN_HOST="$(python3 -c "from urllib.parse import urlparse; print(urlparse('${DFL_STREAM_PUBLISH_URL}').hostname or '')")"
fi
if [[ -z "$TURN_HOST" ]]; then
  TURN_HOST="${EXT_IP:-localhost}"
fi
export DFL_TURN_PUBLIC_HOST="${TURN_HOST}"

HTTP_PORT="${DFL_STREAM_HTTP_PORT:-8080}"
echo "Starting stream publisher + ingest (Python) on 0.0.0.0:${HTTP_PORT}"
export DFL_STREAM_BIND_HOST=0.0.0.0
export DFL_STREAM_HTTP_PORT="${HTTP_PORT}"
python -m services.stream_ingest &
sleep 0.5

exec python main.py run DeepFaceLive --userdata-dir /data/
