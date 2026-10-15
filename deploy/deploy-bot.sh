#!/bin/bash
# Выкатывает версию бота по тегу: git-тег → сборка образа → перезапуск → проверка здоровья → откат при сбое.
# Вызывается из GitHub Actions через SSH-ключ с forced command (см. deploy/README.md) или вручную:
#   sudo /usr/local/sbin/deploy-bot v1.2.3
set -euo pipefail

CONF=/etc/deploy-bot.env
# shellcheck source=/dev/null
. "$CONF"
: "${REPO_URL:?}" "${SRC_DIR:?}" "${APP_DIR:?}" "${CONTAINER:?}"
IMAGE=${IMAGE:-subscription-bot}
METRICS_URL=${METRICS_URL:-http://127.0.0.1:9101/metrics}
KEEP_IMAGES=${KEEP_IMAGES:-3}

TAG="${1:-}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Ожидается тег вида v1.2.3, получено: '${TAG}'"; exit 2; }

exec 9>/run/deploy-bot.lock
flock -n 9 || { echo "Деплой уже выполняется"; exit 3; }

log(){ echo "$(date -u '+%F %T') [$TAG] $*" | tee -a /var/log/deploy-bot.log; }

# 1. исходники нужного тега
if [ ! -d "$SRC_DIR/.git" ]; then git clone --quiet "$REPO_URL" "$SRC_DIR"; fi
git -C "$SRC_DIR" fetch --quiet --force --tags --prune origin
git -C "$SRC_DIR" -c advice.detachedHead=false checkout --quiet --force "refs/tags/$TAG"
log "исходники: $(git -C "$SRC_DIR" rev-parse --short HEAD)"

# 2. сборка
docker build --quiet --label "org.opencontainers.image.version=$TAG" -t "$IMAGE:$TAG" "$SRC_DIR" >/dev/null
log "образ собран: $IMAGE:$TAG"

# 3. переключение: compose использует $IMAGE:current
PREV=$(docker image inspect "$IMAGE:current" --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null || true)
switch(){ docker tag "$IMAGE:$1" "$IMAGE:current" && docker compose --project-directory "$APP_DIR" up -d --no-build >/dev/null 2>&1; }
healthy(){
  local started
  for _ in $(seq 1 30); do
    sleep 3
    [ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$CONTAINER" 2>/dev/null)" = "true 0" ] || continue
    curl -fsS -m 3 "$METRICS_URL" >/dev/null 2>&1 || continue
    # логи с момента старта именно этого контейнера: бот пишет «Start polling» почти сразу после запуска
    started=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER")
    docker logs --since "$started" "$CONTAINER" 2>&1 | grep -q "Start polling" && return 0
  done
  return 1
}

switch "$TAG"
if healthy; then
  log "OK: работает $TAG (было: ${PREV:-нет})"
else
  log "ОШИБКА: $TAG не прошёл проверку здоровья, последние логи:"
  docker logs --tail 30 "$CONTAINER" 2>&1 | tee -a /var/log/deploy-bot.log || true
  if [ -n "$PREV" ] && docker image inspect "$IMAGE:$PREV" >/dev/null 2>&1; then
    if switch "$PREV" && healthy; then log "откат на $PREV выполнен"; else log "ОТКАТ НЕ УДАЛСЯ — нужна ручная проверка"; fi
  fi
  exit 1
fi

# 4. уборка: оставить последние KEEP_IMAGES версий
docker images "$IMAGE" --format '{{.Tag}}' | grep -E '^v[0-9]' | sort -rV | tail -n +$((KEEP_IMAGES + 1)) \
  | while read -r old; do docker rmi "$IMAGE:$old" >/dev/null 2>&1 || true; done
