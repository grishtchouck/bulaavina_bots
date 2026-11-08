# bulaavina_bots

Telegram-боты. Каждый бот лежит в своей папке со своим окружением, `.env` и systemd-сервисом.

| Бот | Папка | Сервис | Порт | Домен вебхука |
|---|---|---|---|---|
| Курс по контенту | `course-bot/` | `course-bot` | 8080 | bot.bulaavina.ru |

## Установка на новый сервер (Debian 12, зарубежная локация)

**1. Доступ к репозиторию (deploy key, только чтение):**

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub   # строку целиком → GitHub: Settings → Deploy keys → Add deploy key
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
apt update && apt install -y git
git clone git@github.com:grishtchouck/bulaavina_bots.git /opt/bulaavina_bots
```

**2. Установка бота одной командой:**

```bash
bash /opt/bulaavina_bots/install.sh course-bot bot.bulaavina.ru 8080
```

Скрипт проверяет доступ к Telegram и сразу останавливается, если его нет. Дальше он настраивает подкачку, пакеты, venv, `.env`, systemd-сервис и Caddy с HTTPS, а также подсказывает, если DNS ещё не указывает на сервер. Скрипт можно запускать повторно.

**3. Токен:** `nano /opt/bulaavina_bots/course-bot/.env` → `BOT_TOKEN`, `ADMIN_IDS` → `systemctl restart course-bot`.

## Обновление после изменений в репозитории

```bash
cd /opt/bulaavina_bots && git pull
systemctl restart course-bot        # если менялся код
# если менялся только scenario.yaml — достаточно /reload в боте
```

## Добавить нового бота

1. Скопировать папку бота (например `course-bot/` → `new-bot/`), поправить сценарий.
2. Добавить в DNS поддомен на IP сервера.
3. На сервере: `git pull && bash install.sh new-bot bot2.bulaavina.ru 8081`. У каждого бота свой порт (8080, 8081…), сервис и Caddy скрипт настроит сам.

## Полезное

```bash
journalctl -u course-bot -f      # логи
systemctl restart course-bot     # перезапуск
systemctl status course-bot      # статус
```
