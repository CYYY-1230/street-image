#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/streetscope}"
DATA_DIR="${DATA_DIR:-/var/lib/streetscope}"
DOMAIN="${DOMAIN:-}"
WEB_USER="${WEB_USER:-admin}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
DEFAULT_SEGMENTATION_URL="${DEFAULT_SEGMENTATION_URL:-}"
PORT="${PORT:-8000}"

if [[ -z "$WEB_PASSWORD" ]]; then
  echo "WEB_PASSWORD is required. Example: WEB_PASSWORD='your-password' bash deploy/server_bootstrap_web.sh" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR/backend" || ! -d "$APP_DIR/frontend" ]]; then
  echo "APP_DIR must contain backend/ and frontend/. Current APP_DIR=$APP_DIR" >&2
  exit 1
fi

apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync curl ca-certificates nodejs npm supervisor debian-keyring debian-archive-keyring apt-transport-https >/tmp/streetscope-web-apt.log

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy >/tmp/streetscope-caddy-apt.log
fi

mkdir -p "$DATA_DIR"
cd "$APP_DIR/frontend"
npm install
cat > .env.production <<CONF
VITE_API_BASE=
VITE_DEFAULT_SEGMENTATION_SERVICE_URL=$DEFAULT_SEGMENTATION_URL
CONF
npm run build

cd "$APP_DIR/backend"
python3 -m venv .venv
.venv/bin/pip install -U pip wheel setuptools -q
.venv/bin/pip install -r requirements.txt -q

cat > /etc/supervisor/conf.d/streetscope-web.conf <<CONF
[program:streetscope-web]
command=$APP_DIR/backend/.venv/bin/uvicorn main:app --host 127.0.0.1 --port $PORT --timeout-keep-alive 30
directory=$APP_DIR/backend
autostart=true
autorestart=true
startsecs=5
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=$APP_DIR/backend/web-supervisor.out.log
stderr_logfile=$APP_DIR/backend/web-supervisor.err.log
stdout_logfile_maxbytes=20MB
stderr_logfile_maxbytes=20MB
stdout_logfile_backups=5
stderr_logfile_backups=5
environment=STREETSCOPE_DATA_DIR="$DATA_DIR",STREETSCOPE_USER="$WEB_USER",STREETSCOPE_PASSWORD="$WEB_PASSWORD",STREETSCOPE_FRONTEND_DIST="$APP_DIR/frontend/dist",STREETSCOPE_CORS_ORIGINS="https://$DOMAIN,http://$DOMAIN,http://127.0.0.1:5173,http://localhost:5173"
CONF

if [[ -n "$DOMAIN" ]]; then
  site_label="$DOMAIN"
else
  site_label=":80"
fi

cat > /etc/caddy/Caddyfile <<CONF
$site_label {
  encode gzip zstd
  reverse_proxy 127.0.0.1:$PORT
}
CONF

supervisorctl reread
supervisorctl update
supervisorctl restart streetscope-web || true
systemctl reload caddy || systemctl restart caddy
sleep 3
curl -fsS "http://127.0.0.1:$PORT/api/health"
echo
echo "StreetScope web is ready."
if [[ -n "$DOMAIN" ]]; then
  echo "Open: https://$DOMAIN"
else
  echo "Open: http://<server-ip>"
fi
