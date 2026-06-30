#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ ! -f ".env" ]; then
  echo "Missing deploy/nas/.env. Copy .env.example to .env and fill secrets first."
  exit 1
fi

echo "Pulling latest StreetScope Docker images..."
docker compose -f docker-compose.ghcr.yml pull

echo "Restarting StreetScope NAS services..."
docker compose -f docker-compose.ghcr.yml up -d

echo "Done. Current containers:"
docker compose -f docker-compose.ghcr.yml ps
