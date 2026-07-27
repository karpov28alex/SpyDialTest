#!/usr/bin/env bash
set -euo pipefail
cd /opt/dialog-spy

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Восстанавливаем чистую базовую структуру из архива, который хранится в Git,
# не затрагивая .env, PostgreSQL/Redis volumes и media volume.
git show HEAD:dialog-spy-python-mvp.zip > "$TMP_DIR/base.zip"
unzip -oq "$TMP_DIR/base.zip" -d "$TMP_DIR/base"
cp -a "$TMP_DIR/base/dialog-spy/." /opt/dialog-spy/

cat release-0.3.0-parts/part00 release-0.3.0-parts/part01 release-0.3.0-parts/part02 release-0.3.0-parts/part03 \
  | base64 -d | gzip -d > "$TMP_DIR/v030.patch"
patch -d /opt/dialog-spy -p4 --forward < "$TMP_DIR/v030.patch"

# Версия задаётся серверным .env, поэтому обновляем её отдельно.
if grep -q '^APP_VERSION=' .env; then
  sed -i 's/^APP_VERSION=.*/APP_VERSION=0.3.0/' .env
else
  echo 'APP_VERSION=0.3.0' >> .env
fi

chmod 600 .env
docker compose up -d --build --force-recreate
sleep 12
docker compose ps
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
