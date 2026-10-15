#!/bin/bash
# Еженедельное обновление geo-списков (runetfreedom) на всех нодах: geosite_RU.dat и geoip_RU.dat.
# Скачивает, проверяет xray'ем, раскладывает по нодам, перезагружает xray через API панели,
# проверяет, что порты после перезагрузки отвечают. При любом сбое пишет администратору в Telegram.
# Настройки: geo-update.env (пример — geo-update.env.example), путь можно передать через CONF.
set -uo pipefail
CONF=${CONF:-/etc/geo-update/geo-update.env}
# shellcheck source=/dev/null
. "$CONF" || { echo "нет файла настроек $CONF"; exit 1; }
: "${WORK_DIR:?}" "${BOT_ENV:?}" "${PANEL_URL:?}" "${NODE_IDS:?}" "${LOCAL_NODE:?}" "${CHECK_TARGETS:?}"
BASE=https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release
LOG=$WORK_DIR/update.log

val(){ grep "^$1=" "$BOT_ENV" | cut -d= -f2-; }
log(){ echo "$(date -u '+%F %T') $*" | tee -a "$LOG"; }
alert(){
  log "ОШИБКА: $*"
  curl -s -m 15 -o /dev/null "https://api.telegram.org/bot$(val BOT_TOKEN)/sendMessage" \
    --data-urlencode "chat_id=$(val ADMIN_ID)" --data-urlencode "text=⚠️ Обновление geo-списков: $*" || true
  exit 1
}
cd "$WORK_DIR" || exit 1
W=$(mktemp -d "$WORK_DIR/work.XXXXXX"); trap 'rm -rf "$W"' EXIT

curl -fsSL -m 600 --retry 3 -o "$W/geosite_RU.dat" "$BASE/geosite.dat" || alert "не скачался geosite.dat"
curl -fsSL -m 600 --retry 3 -o "$W/geoip_RU.dat"   "$BASE/geoip.dat"   || alert "не скачался geoip.dat"
[ "$(stat -c %s "$W/geosite_RU.dat")" -gt 20000000 ] || alert "geosite.dat подозрительно мал"
[ "$(stat -c %s "$W/geoip_RU.dat")"   -gt 5000000 ]  || alert "geoip.dat подозрительно мал"

# xray должен загрузить именно те категории, которые используются в маршрутизации
cat > "$W/t.json" <<'J'
{"log":{"loglevel":"error"},"outbounds":[{"tag":"x","protocol":"freedom"}],"routing":{"rules":[
 {"type":"field","domain":["ext:geosite_RU.dat:ru-blocked","ext:geosite_RU.dat:ru-available-only-inside"],"outboundTag":"x"},
 {"type":"field","ip":["ext:geoip_RU.dat:ru-blocked","ext:geoip_RU.dat:telegram"],"outboundTag":"x"}]}}
J
XRAY_LOCATION_ASSET="$W" "$WORK_DIR/xray" run -test -c "$W/t.json" 2>&1 | grep -q 'Configuration OK' \
  || alert "новые файлы не прошли проверку xray — оставлены старые"

NEW="$(sha256sum "$W/geosite_RU.dat" "$W/geoip_RU.dat" | awk '{print $1}' | tr '\n' ' ')"
if [ "$NEW" = "$(cat "$WORK_DIR/installed.sha" 2>/dev/null)" ]; then log "списки не изменились — пропуск"; exit 0; fi

for f in geosite_RU.dat geoip_RU.dat; do
  # локальная нода: копирование во временный файл и атомарная замена
  docker cp "$W/$f" "$LOCAL_NODE:/app/bin/$f.new" >/dev/null \
    && docker exec "$LOCAL_NODE" mv "/app/bin/$f.new" "/app/bin/$f" || alert "не удалось положить $f на локальную ноду"
  # удалённая нода (необязательно): REMOTE_EXEC — команда, выполняющая аргумент на удалённом сервере, например через ssh
  if [ -n "${REMOTE_EXEC:-}" ]; then
    $REMOTE_EXEC "docker exec -i $REMOTE_NODE sh -c 'cat > /app/bin/$f.new && mv /app/bin/$f.new /app/bin/$f'" < "$W/$f" \
      || alert "не удалось передать $f на удалённую ноду"
    size=$($REMOTE_EXEC "docker exec $REMOTE_NODE stat -c %s /app/bin/$f" | tail -1)
    [ "$size" = "$(stat -c %s "$W/$f")" ] || alert "на удалённой ноде $f записан не полностью ($size байт)"
  fi
done

TOKEN=$(val PANEL_API_TOKEN)
for id in $NODE_IDS; do
  curl -s -m 60 -X POST -H "Authorization: Bearer $TOKEN" "$PANEL_URL/panel/node/reload/$id" \
    | grep -q '"success":true' || alert "нода $id не перезагрузилась"
done
sleep 20
for t in $CHECK_TARGETS; do
  timeout 6 bash -c "</dev/tcp/${t%:*}/${t##*:}" 2>/dev/null || alert "после обновления не отвечает $t"
done
echo "$NEW" > "$WORK_DIR/installed.sha"
log "обновлено: geosite_RU $(stat -c %s "$W/geosite_RU.dat") байт, geoip_RU $(stat -c %s "$W/geoip_RU.dat") байт"
