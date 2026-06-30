#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/streetscope-segmentation}"
MMSEG_DIR="${MMSEG_DIR:-/root/streetscope-mmseg-service}"
MMSEG_MODEL_ROOT="${MMSEG_MODEL_ROOT:-/root/street_models/mmsegmentation}"
HOST="${HOST:-0.0.0.0}"
MAIN_PORT="${MAIN_PORT:-9000}"
MMSEG_PORT="${MMSEG_PORT:-9001}"

apt-get update -qq
apt-get install -y -qq libgl1 libglib2.0-0 >/tmp/streetscope-apt.log

cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -U pip wheel setuptools -q
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python3 predownload_models.py --include-mmseg --mmseg-root "$MMSEG_MODEL_ROOT"

mkdir -p "$MMSEG_DIR"
python3 -m venv "$MMSEG_DIR/.venv"
"$MMSEG_DIR/.venv/bin/pip" install -U pip wheel setuptools -q
"$MMSEG_DIR/.venv/bin/pip" install -r "$APP_DIR/mmseg_requirements.txt" -q

cat > "$APP_DIR/run_supervised.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/uvicorn" app:app --host "$HOST" --port "$MAIN_PORT" --timeout-keep-alive 30
SH
chmod +x "$APP_DIR/run_supervised.sh"

cat > /etc/supervisor/conf.d/streetscope-segmentation.conf <<CONF
[program:streetscope-segmentation]
command=$APP_DIR/run_supervised.sh
directory=$APP_DIR
autostart=true
autorestart=true
startsecs=8
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=$APP_DIR/supervisor.out.log
stderr_logfile=$APP_DIR/supervisor.err.log
stdout_logfile_maxbytes=20MB
stderr_logfile_maxbytes=20MB
stdout_logfile_backups=5
stderr_logfile_backups=5
environment=HF_ENDPOINT="https://hf-mirror.com",HF_HOME="/root/.cache/huggingface",TRANSFORMERS_CACHE="/root/.cache/huggingface",MMSEG_MODEL_ROOT="$MMSEG_MODEL_ROOT",MMSEG_SERVICE_URL="http://127.0.0.1:$MMSEG_PORT/segment",MMSEG_HEALTH_URL="http://127.0.0.1:$MMSEG_PORT/health"
CONF

cat > /etc/supervisor/conf.d/streetscope-mmseg-sidecar.conf <<CONF
[program:streetscope-mmseg-sidecar]
command=$MMSEG_DIR/.venv/bin/uvicorn mmseg_sidecar:app --host 127.0.0.1 --port $MMSEG_PORT --timeout-keep-alive 30
directory=$APP_DIR
autostart=true
autorestart=true
startsecs=8
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=$APP_DIR/mmseg-sidecar.out.log
stderr_logfile=$APP_DIR/mmseg-sidecar.err.log
stdout_logfile_maxbytes=20MB
stderr_logfile_maxbytes=20MB
stdout_logfile_backups=5
stderr_logfile_backups=5
environment=MMSEG_MODEL_ROOT="$MMSEG_MODEL_ROOT"
CONF

supervisorctl reread
supervisorctl update
supervisorctl restart streetscope-mmseg-sidecar || true
supervisorctl restart streetscope-segmentation || true
sleep 8
curl -fsS "http://127.0.0.1:$MAIN_PORT/health"
