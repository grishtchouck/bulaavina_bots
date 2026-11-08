#!/usr/bin/env bash
# Установка/обновление бота на чистом Debian 12.
#
#   bash install.sh <папка-бота> <домен-вебхука> [порт]
#   bash install.sh course-bot bot.bulaavina.ru 8080
#
# Скрипт можно запускать повторно: уже сделанные шаги пропускаются,
# существующий .env не перезаписывается.
set -euo pipefail

BOT="${1:-}"; DOMAIN="${2:-}"; PORT="${3:-8080}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$REPO_DIR/$BOT"

ok()   { echo -e "\033[32m✔\033[0m $*"; }
info() { echo -e "\033[36m→\033[0m $*"; }
die()  { echo -e "\033[31m✘ $*\033[0m" >&2; exit 1; }

[[ -n "$BOT" && -n "$DOMAIN" ]] || die "Использование: bash install.sh <папка-бота> <домен> [порт]
  пример: bash install.sh course-bot bot.bulaavina.ru 8080"
[[ $EUID -eq 0 ]] || die "Запустите от root"
[[ -f "$BOT_DIR/bot/main.py" ]] || die "Не найден $BOT_DIR/bot/main.py — проверьте имя папки"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "Порт должен быть числом"

# ---------- 1. Доступ к Telegram — проверяем первым делом ----------
info "Проверяю доступ к api.telegram.org…"
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl >/dev/null; }
code=$(curl -4 -m 10 -s -o /dev/null -w "%{http_code}" https://api.telegram.org || true)
if [[ "$code" == "000" ]]; then
  die "Сервер не может подключиться к api.telegram.org — бот здесь работать не будет.
  Нужен VPS в другой локации (Нидерланды, Германия, Финляндия) или прокси."
fi
ok "Telegram доступен (HTTP $code)"

# ---------- 2. Подкачка ----------
if [[ -z "$(swapon --show)" ]]; then
  info "Создаю файл подкачки 1 ГБ…"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "Подкачка включена"
else
  ok "Подкачка уже есть"
fi

# ---------- 3. Пакеты ----------
info "Ставлю пакеты (python3-venv, git, caddy)…"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git caddy >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "Нужен Python 3.10+, а стоит $(python3 --version). Переустановите ОС на Debian 12."
ok "Пакеты установлены, $(python3 --version)"

# ---------- 4. Окружение Python ----------
info "Ставлю зависимости бота…"
[[ -d "$BOT_DIR/.venv" ]] || python3 -m venv "$BOT_DIR/.venv"
"$BOT_DIR/.venv/bin/pip" install -q --upgrade pip
"$BOT_DIR/.venv/bin/pip" install -q -r "$BOT_DIR/requirements.txt"
ok "Зависимости установлены"

# ---------- 5. .env ----------
ENV="$BOT_DIR/.env"
if [[ ! -f "$ENV" ]]; then
  cp "$BOT_DIR/.env.example" "$ENV"
  ok "Создан .env из шаблона"
fi
chmod 600 "$ENV"
sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://$DOMAIN#; s#^WEB_PORT=.*#WEB_PORT=$PORT#" "$ENV"
ok ".env: PUBLIC_URL=https://$DOMAIN, WEB_PORT=$PORT"

# ---------- 6. systemd ----------
cat > "/etc/systemd/system/$BOT.service" <<EOF
[Unit]
Description=Telegram bot: $BOT
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$BOT"
ok "Сервис $BOT зарегистрирован"

# ---------- 7. Caddy (HTTPS для вебхука) ----------
CADDY=/etc/caddy/Caddyfile
# убираем дефолтный конфиг Caddy, если он ещё стоит
if grep -q '^:80' "$CADDY" 2>/dev/null; then : > "$CADDY"; fi
if ! grep -q "^$DOMAIN " "$CADDY" 2>/dev/null; then
  echo "$DOMAIN { reverse_proxy 127.0.0.1:$PORT }" >> "$CADDY"
fi
systemctl reload caddy || systemctl restart caddy
ok "Caddy: https://$DOMAIN → 127.0.0.1:$PORT"

SERVER_IP=$(curl -4 -m 5 -s https://ifconfig.me || echo "?")
DNS_IP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
if [[ "$DNS_IP" != "$SERVER_IP" ]]; then
  echo -e "\033[33m!\033[0m DNS: $DOMAIN → ${DNS_IP:-не найден}, а IP сервера $SERVER_IP."
  echo "  Поменяйте A-запись в DNS. Сертификат Caddy выпустит сам, когда DNS обновится."
fi

# ---------- 8. Запуск ----------
if grep -q '^BOT_TOKEN=123456:ABC' "$ENV" || grep -q '^BOT_TOKEN=$' "$ENV"; then
  echo
  echo "Осталось вписать токен и ID админов:"
  echo "  nano $ENV"
  echo "  systemctl restart $BOT && journalctl -u $BOT -f"
else
  systemctl restart "$BOT"
  sleep 3
  if systemctl is-active -q "$BOT"; then
    ok "Бот запущен. Логи: journalctl -u $BOT -f"
  else
    die "Бот не запустился, смотрите: journalctl -u $BOT -n 50"
  fi
fi
