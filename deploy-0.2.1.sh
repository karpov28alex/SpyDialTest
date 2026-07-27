#!/usr/bin/env bash
set -euo pipefail
cd /opt/dialog-spy
base64 -d release-0.2.1.tar.gz.b64 > /tmp/release-0.2.1.tar.gz
echo '242e7586d37ad8da733429f9e43509ab74e365ce59c816cea31ef64afa54a566  /tmp/release-0.2.1.tar.gz' | sha256sum -c -
tar -xzf /tmp/release-0.2.1.tar.gz -C /opt/dialog-spy
rm -f /tmp/release-0.2.1.tar.gz
docker compose up -d --build --force-recreate
sleep 10
docker compose ps
