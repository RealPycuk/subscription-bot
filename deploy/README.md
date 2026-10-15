# Деплой из GitHub Actions

```
git tag v1.2.3 → push ──► Actions: CI (все проверки) ──► SSH: deploy@server "v1.2.3"
                                                              │
                         forced command: deploy-bot-ssh ──► sudo deploy-bot v1.2.3
                                                              │
     git checkout тега → docker build → retag :current → compose up → health check
                                                              │
                                    не поднялся за 90 с ──► откат на прошлую версию
```

## Безопасность доступа

- Отдельный пользователь `deploy`: пароль заблокирован, ключ в `authorized_keys` с `command=`, `no-pty`, `no-port-forwarding`, `no-agent-forwarding` — получить shell или туннель с этим ключом нельзя.
- Единственное, что можно передать, — тег; скрипт принимает только `^v\d+\.\d+\.\d+$`, всё остальное отклоняется.
- `sudo` разрешён только на `/usr/local/sbin/deploy-bot` (`/etc/sudoers.d/deploy-bot`, проверяется `visudo -c`).
- Код берётся из публичного репозитория по тегу, а не присылается из CI: ключ не позволяет выкатить ничего, чего нет в репозитории.
- В GitHub: ключ, адрес и отпечаток сервера — в секретах; `StrictHostKeyChecking=yes`; деплой в environment `production` и только после успешного CI.

## Проверка здоровья

Новая версия считается рабочей, если в течение 90 секунд: контейнер запущен и ни разу не перезапускался, отвечает `/metrics`, в логах есть `Start polling`. Иначе образ `:current` возвращается на предыдущую версию и контейнер пересоздаётся. Хранятся 3 последние версии образа.

## Настройка сервера

```bash
sudo cp deploy/deploy-bot.env.example /etc/deploy-bot.env   # пути и имя контейнера
sudo bash deploy/setup-server.sh                            # пользователь deploy, ключ, sudoers
```

В `docker-compose.yml` на сервере вместо `build: .` указывается `image: subscription-bot:current`.

Секреты репозитория: `DEPLOY_HOST`, `DEPLOY_SSH_KEY` (приватный ключ `/root/.ssh/github_deploy`), `DEPLOY_KNOWN_HOSTS` (`ssh-keyscan -t ed25519 <host>`).
