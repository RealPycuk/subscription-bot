#!/bin/bash
# Записывает одно значение в .env:  scripts/set-env.sh ИМЯ ЗНАЧЕНИЕ
set -euo pipefail
F=${BOT_ENV:-/opt/subscription-bot/.env}; K="${1:-}"; V="${2:-}"
case "$K" in BOT_TOKEN|ADMIN_ID|PANEL_API_TOKEN|CRYPTOBOT_TOKEN|TRIAL_DAYS|MAX_DEVICES) ;; *) echo "Неизвестное поле: $K"; exit 1;; esac
[ -n "$V" ] || { echo "Пустое значение"; exit 1; }
python3 -I - "$F" "$K" "$V" <<'PY'
import sys,re
f,k,v=sys.argv[1:4]; s=open(f).read()
s,n=re.subn(r'(?m)^'+re.escape(k)+r'=.*$', lambda m: k+'='+v, s)
if not n: s+=f"\n{k}={v}\n"
open(f,'w').write(s)
PY
chmod 600 "$F"; echo "OK: $K записан (длина значения: ${#V})"
