#!/usr/bin/env bash
# Kéo code mới và build lại (giữ nguyên .env và dữ liệu).
#   cd agriguard && sudo bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
PROFILES="--profile datadog"
[ -f deploy/.profiles ] && . deploy/.profiles && PROFILES="$COMPOSE_PROFILES_USED"
# shellcheck disable=SC2086
docker compose -f docker-compose.yml -f docker-compose.prod.yml $PROFILES up -d --build --remove-orphans
docker image prune -f >/dev/null
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8000/api/health && echo && exit 0; sleep 3; done
echo "Backend chưa trả lời – xem: docker compose logs backend --tail 80"; exit 1
