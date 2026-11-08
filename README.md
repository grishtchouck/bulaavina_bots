# bulaavina_bots

Telegram-боты. Каждый бот лежит в своей папке со своим окружением, `.env` и systemd-сервисом.

| Бот | Папка | Сервис | Порт | Домен вебхука |
|---|---|---|---|---|
| Курс по контенту | `course-bot/` | `course-bot` | 8080 | bot.bulaavina.ru |

## Первичная настройка сервера (один раз)

```bash
# подкачка 1 ГБ
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
# пакеты
apt update && apt install -y python3-venv git caddy
```

Доступ сервера к приватному репозиторию — deploy key (только чтение):

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub   # → GitHub: Settings → Deploy keys → Add deploy key
cat >> ~/.ssh/config <<'CFG'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
CFG
git clone git@github.com:grishtchouck/bulaavina_bots.git /opt/bulaavina_bots
```

## Обновление после изменений в репозитории

```bash
cd /opt/bulaavina_bots && git pull
systemctl restart course-bot        # если менялся код
# если менялся только scenario.yaml — достаточно /reload в боте
```

## Добавить нового бота

1. Скопировать папку бота (например `course-bot/` → `new-bot/`), поправить сценарий.
2. В `new-bot/new-bot.service` заменить `course-bot` на `new-bot`.
3. На сервере: venv + `.env` с новым токеном и **другим** `WEB_PORT` (8081, 8082…).
4. Добавить поддомен в DNS и строку в `/etc/caddy/Caddyfile`:
   ```
   bot.bulaavina.ru  { reverse_proxy 127.0.0.1:8080 }
   bot2.bulaavina.ru { reverse_proxy 127.0.0.1:8081 }
   ```
   затем `systemctl reload caddy`.

## Полезное

```bash
journalctl -u course-bot -f      # логи
systemctl restart course-bot     # перезапуск
systemctl status course-bot      # статус
```
