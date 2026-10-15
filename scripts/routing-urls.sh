#!/bin/bash
# Еженедельно: подставляет в профиль маршрутизации подписки (RU-direct) свежие ссылки на geo-файлы RoscomVPN.
# Ссылки берутся из их профиля DEFAULT.JSON, файлы скачиваются и проверяются xray'ем на категории из наших правил.
# Профиль меняется только если ссылки изменились и проверка прошла. При сбое — сообщение админу в Telegram.
set -uo pipefail
D=${WORK_DIR:-/opt/geo-update}; LOG=$D/update.log; ENV=${BOT_ENV:-/opt/subscription-bot/.env}
SRC=https://raw.githubusercontent.com/hydraponique/roscomvpn-routing/refs/heads/main/HAPP/DEFAULT.JSON
val(){ grep "^$1=" "$ENV" | cut -d= -f2-; }
log(){ echo "$(date -u '+%F %T') [routing] $*" | tee -a "$LOG"; }
alert(){ log "ОШИБКА: $*"; curl -s -m 15 -o /dev/null "https://api.telegram.org/bot$(val BOT_TOKEN)/sendMessage" \
  --data-urlencode "chat_id=$(val ADMIN_ID)" --data-urlencode "text=⚠️ Обновление ссылок на geo-файлы в подписке: $*" || true; exit 1; }
psql(){ docker exec -i sharx_postgres psql -U xui_user -d xui_db -At -v ON_ERROR_STOP=1 "$@"; }
W=$(mktemp -d "$D/rt.XXXXXX"); trap 'rm -rf "$W"' EXIT

curl -fsSL -m 60 --retry 3 -o "$W/src.json" "$SRC" || alert "не скачался профиль RoscomVPN"
read -r GEOIP GEOSITE < <(python3 -I -c "
import json,sys; d=json.load(open(sys.argv[1]))
ok=lambda u: isinstance(u,str) and u.startswith('https://') and u.endswith('.dat')
assert ok(d['Geoipurl']) and ok(d['Geositeurl'])
print(d['Geoipurl'], d['Geositeurl'])" "$W/src.json") || alert "в профиле RoscomVPN нет корректных ссылок"

psql -c "select config_json#>>'{routing,profiles,0,body}' from subscription_page_configs where uuid='00000000-0000-0000-0000-000000000000'" > "$W/cur.json"
read -r CUR_IP CUR_SITE NAME < <(python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['Geoipurl'], d['Geositeurl'], d['Name'])" "$W/cur.json") \
  || alert "не удалось прочитать текущий профиль подписки"
[ "$NAME" = "RU-direct" ] || alert "первый профиль маршрутизации называется «$NAME», а не RU-direct — не трогаю"
if [ "$GEOIP" = "$CUR_IP" ] && [ "$GEOSITE" = "$CUR_SITE" ]; then log "ссылки не изменились — пропуск"; exit 0; fi

curl -fsSL -m 120 --retry 3 -o "$W/geoip.dat"   "$GEOIP"   || alert "не скачался geoip.dat по новой ссылке"
curl -fsSL -m 120 --retry 3 -o "$W/geosite.dat" "$GEOSITE" || alert "не скачался geosite.dat по новой ссылке"
[ "$(stat -c %s "$W/geoip.dat")" -gt 100000 ] && [ "$(stat -c %s "$W/geosite.dat")" -gt 20000 ] || alert "новые geo-файлы подозрительно малы"

# все категории, которые используются в профиле RU-direct, должны существовать в новых файлах
python3 -I - "$W/cur.json" "$W/t.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1]))
dom=[x for x in b["DirectSites"]+b["ProxySites"]+b["BlockSites"] if x.startswith("geosite:")]
ip=[x for x in b["DirectIp"]+b["ProxyIp"]+b["BlockIp"] if x.startswith("geoip:")]
rules=[]
if dom: rules.append({"type":"field","domain":dom,"outboundTag":"x"})
if ip:  rules.append({"type":"field","ip":ip,"outboundTag":"x"})
json.dump({"log":{"loglevel":"error"},"outbounds":[{"tag":"x","protocol":"freedom"}],"routing":{"rules":rules}},open(sys.argv[2],"w"))
PY
XRAY_LOCATION_ASSET="$W" "$D/xray" run -test -c "$W/t.json" 2>&1 | grep -q 'Configuration OK' \
  || alert "в новых geo-файлах нет нужных категорий — оставлены старые ссылки"

python3 -I - "$W/cur.json" "$GEOIP" "$GEOSITE" "$W/upd.sql" <<'PY'
import json,sys,time
b=json.load(open(sys.argv[1])); b["Geoipurl"]=sys.argv[2]; b["Geositeurl"]=sys.argv[3]; b["LastUpdated"]=str(int(time.time()))
s=json.dumps(b,ensure_ascii=False,indent=2); assert "$b$" not in s
open(sys.argv[4],"w").write(f"""begin;
update subscription_page_configs set config_json=jsonb_set(config_json,'{{routing,profiles,0,body}}',to_jsonb($b${s}$b$::text)),
  updated_at=extract(epoch from now())::bigint
 where uuid='00000000-0000-0000-0000-000000000000' and config_json#>>'{{routing,profiles,0,name}}'='RU-direct';
commit;
""")
PY
psql < "$W/upd.sql" >/dev/null || alert "не удалось записать новые ссылки в профиль"
log "ссылки обновлены: geoip ${GEOIP##*@} geosite ${GEOSITE##*@}"
