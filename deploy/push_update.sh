#!/usr/bin/env bash
set -euo pipefail

SERVER_HOST="${SERVER_HOST:-}"
SERVER_USER="${SERVER_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
APP_DIR="${APP_DIR:-/opt/streetscope}"
DOMAIN="${DOMAIN:-}"
WEB_USER="${WEB_USER:-admin}"
WEB_PASSWORD="${WEB_PASSWORD:-}"
DATA_DIR="${DATA_DIR:-/var/lib/streetscope}"
DEFAULT_SEGMENTATION_URL="${DEFAULT_SEGMENTATION_URL:-}"

if [[ -z "$SERVER_HOST" ]]; then
  echo "SERVER_HOST is required. Example: SERVER_HOST=1.2.3.4 SSH_PORT=22 WEB_PASSWORD='xxx' bash deploy/push_update.sh" >&2
  exit 1
fi
if [[ -z "$WEB_PASSWORD" ]]; then
  echo "WEB_PASSWORD is required. Use the same login password you want for the website." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh -p "$SSH_PORT" "$SERVER_USER@$SERVER_HOST" "mkdir -p '$APP_DIR'"
rsync -az --delete \
  --exclude '.DS_Store' \
  --exclude 'frontend/node_modules' \
  --exclude 'frontend/dist' \
  --exclude 'backend/.venv' \
  --exclude 'backend/data' \
  --exclude 'segmentation_service/.venv' \
  --exclude 'SVC 街景爬取视频教程' \
  -e "ssh -p $SSH_PORT" \
  "$ROOT_DIR/" "$SERVER_USER@$SERVER_HOST:$APP_DIR/"

ssh -p "$SSH_PORT" "$SERVER_USER@$SERVER_HOST" \
  "cd '$APP_DIR' && DOMAIN='$DOMAIN' WEB_USER='$WEB_USER' WEB_PASSWORD='$WEB_PASSWORD' DATA_DIR='$DATA_DIR' DEFAULT_SEGMENTATION_URL='$DEFAULT_SEGMENTATION_URL' APP_DIR='$APP_DIR' bash deploy/server_bootstrap_web.sh"

echo "Deploy complete."
if [[ -n "$DOMAIN" ]]; then
  echo "Open: https://$DOMAIN"
else
  echo "Open: http://$SERVER_HOST"
fi
