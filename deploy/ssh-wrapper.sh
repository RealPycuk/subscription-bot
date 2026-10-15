#!/bin/bash
# Forced command для ключа деплоя в ~deploy/.ssh/authorized_keys:
# ключ может только передать тег; всё остальное отклоняется.
set -euo pipefail
exec sudo -n /usr/local/sbin/deploy-bot "${SSH_ORIGINAL_COMMAND:-}"
