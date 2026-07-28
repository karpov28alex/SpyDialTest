#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/dialog-spy}"
BACKUP_DIR="${BACKUP_DIR:-/root/dialog-spy-deploy-backups}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health/ready}"

cd "$PROJECT_DIR"
mkdir -p "$BACKUP_DIR"

if [[ ! -f .env ]]; then
  echo "ERROR: $PROJECT_DIR/.env is missing" >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cp .env "$BACKUP_DIR/.env.$STAMP"

echo "[1/8] Fetching source"
git fetch --prune origin

echo "[2/8] Validating compose configuration"
docker compose config --quiet

echo "[3/8] Building immutable images"
docker compose build --pull api worker migrate

echo "[4/8] Running migrations"
docker compose run --rm migrate

echo "[5/8] Starting services"
docker compose up -d --remove-orphans postgres redis api worker

echo "[6/8] Waiting for API readiness"
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "$HEALTH_URL" >/dev/null; then
    break
  fi
  sleep 2
done
curl --fail --silent --show-error "$HEALTH_URL"
echo

echo "[7/8] Configuring Telegram webhook"
docker compose exec -T api python scripts/set_webhook.py

echo "[8/8] Final status"
docker compose ps

echo "Deployment completed at $STAMP"
