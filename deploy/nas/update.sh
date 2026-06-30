#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

cd "$REPO_DIR"

if [ ! -d ".git" ]; then
  echo "This folder is not a git clone."
  echo "Use git clone on the NAS if you want one-command updates:"
  echo "  git clone https://github.com/CYYY-1230/street-image.git"
  exit 1
fi

echo "Pulling latest StreetScope code..."
git pull --ff-only

cd "$SCRIPT_DIR"

if [ ! -f ".env" ]; then
  echo "Missing deploy/nas/.env. Copy .env.example to .env and fill secrets first."
  exit 1
fi

echo "Rebuilding and restarting NAS containers..."
docker compose up -d --build

echo "Done. Current containers:"
docker compose ps
