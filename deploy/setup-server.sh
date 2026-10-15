#!/bin/bash
# Подготовка сервера к деплою из GitHub Actions. Запуск: sudo bash deploy/setup-server.sh
# Создаёт пользователя deploy без пароля и shell-доступа, ключ с forced command, sudo только на скрипт деплоя.
set -euo pipefail
cd "$(dirname "$0")"
[ -f /etc/deploy-bot.env ] || { echo "Сначала создайте /etc/deploy-bot.env (пример: deploy-bot.env.example)"; exit 1; }

install -m 755 deploy-bot.sh /usr/local/sbin/deploy-bot
install -m 755 ssh-wrapper.sh /usr/local/sbin/deploy-bot-ssh
install -m 440 sudoers-deploy /etc/sudoers.d/deploy-bot
visudo -cf /etc/sudoers.d/deploy-bot

id deploy >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash deploy
passwd -l deploy >/dev/null
install -d -m 700 -o deploy -g deploy ~deploy/.ssh

KEY=/root/.ssh/github_deploy
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy" -f "$KEY"
OPTS='command="/usr/local/sbin/deploy-bot-ssh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty'
echo "$OPTS $(cat "$KEY.pub")" > ~deploy/.ssh/authorized_keys
chown deploy:deploy ~deploy/.ssh/authorized_keys && chmod 600 ~deploy/.ssh/authorized_keys
echo "Готово. Приватный ключ для секрета DEPLOY_SSH_KEY: $KEY"
