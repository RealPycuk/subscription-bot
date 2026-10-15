#!/bin/bash
# Минимальный набор правил файрвола. Порты служебных мостов открыты только для второго сервера.
# Важно: Docker публикует порты в обход ufw, поэтому порты контейнеров привязываются к 127.0.0.1.
set -euo pipefail
PEER=${PEER:?укажите IP второго сервера: PEER=198.51.100.20}

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp          comment 'ssh'
ufw allow 80/tcp          comment 'acme http-01'
ufw allow 443/tcp         comment 'reality'
ufw allow 449/tcp         comment 'reality xhttp'
ufw allow 444/udp         comment 'hysteria2'
ufw allow 2053/tcp        comment 'panel'
ufw allow 2096/tcp        comment 'subscriptions'
ufw allow from "$PEER" to any port 8444 proto tcp comment 'bridge from peer'
ufw --force enable
ufw status verbose
