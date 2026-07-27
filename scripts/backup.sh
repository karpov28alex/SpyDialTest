#!/usr/bin/env sh
set -eu
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p backups/$STAMP
docker compose exec -T postgres pg_dump -U dialog_spy -Fc dialog_spy > "backups/$STAMP/postgres.dump"
docker run --rm -v dialog-spy_media_data:/source:ro -v "$PWD/backups/$STAMP:/backup" alpine sh -c 'tar czf /backup/media.tar.gz -C /source .'
cp .env.example "backups/$STAMP/env.schema"
echo "Backup created: backups/$STAMP"
