#!/bin/bash
# Защита SSH: fail2ban с нарастающим баном + вход только по ключу.
# Пароль отключается ТОЛЬКО если в журнале уже есть успешный вход по ключу — иначе можно потерять доступ.
# Запуск из корня репозитория: sudo bash server/harden-ssh.sh
set -euo pipefail
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq && apt-get install -y -qq fail2ban >/dev/null
install -m 644 fail2ban/jail.local /etc/fail2ban/jail.local
install -m 644 ssh/60-hardening.conf /etc/ssh/sshd_config.d/60-hardening.conf

K=/root/.ssh/authorized_keys
if [ -s "$K" ] && journalctl -u ssh --since '3 days ago' 2>/dev/null | grep -q 'Accepted publickey'; then
  install -m 644 ssh/10-keys-only.conf /etc/ssh/sshd_config.d/10-keys-only.conf
  echo "вход по паролю отключён"
else
  echo "ВНИМАНИЕ: ещё не было входа по ключу — пароль оставлен включённым"
fi

sshd -t                                 # при ошибке в конфиге скрипт остановится до перезагрузки sshd
systemctl reload ssh 2>/dev/null || systemctl reload sshd
systemctl enable --now fail2ban >/dev/null 2>&1
systemctl restart fail2ban
sleep 2
fail2ban-client status sshd
sshd -T | grep -E '^(passwordauthentication|permitrootlogin|maxauthtries) '
