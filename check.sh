#!/bin/sh
# Проверка перед запуском на Linux-сервере:  sh check.sh
# Смотрит Git, Docker, файлы проекта, .env, память и диск, затем запускает проверку внутри контейнера:
# БД, доступ к 2ГИС по каждому прокси, API отзывов, Chrome.
cd "$(dirname "$0")" || exit 1
fail=0
ok()   { echo "  OK   $1"; }
bad()  { echo "  FAIL $1  ->  $2"; fail=$((fail + 1)); }
check() { if eval "$1"; then ok "$2"; else bad "$2" "$3"; fi; }

echo "Проверка сервера"
check "command -v git >/dev/null" "git установлен" "установите git"
check "command -v docker >/dev/null" "docker установлен" "установите Docker Engine"
check "docker info >/dev/null 2>&1" "Docker запущен и доступен этому пользователю" "sudo systemctl start docker; добавьте пользователя в группу docker"
check "docker compose version >/dev/null 2>&1" "docker compose доступен" "установите плагин docker-compose-plugin"

for f in Dockerfile docker-compose.yml docker-entrypoint.sh requirements.txt main.py parser.py fetcher.py storage.py sql/schema.sql; do
    check "[ -f $f ]" "файл $f" "git pull"
done
check "[ -f .env ]" "файл .env" "cp .env.example .env и заполните PG_DSN"

if [ -f .env ]; then
    dsn=$(grep '^PG_DSN=' .env | cut -d= -f2-)
    check "[ -n \"$dsn\" ] && ! echo \"$dsn\" | grep -q 'password@localhost'" "PG_DSN заполнен" "впишите строку подключения к вашей БД"
    echo "  ...  прокси в .env: $(grep -cE '^DGIS_PROXY(_[0-9])?=[^[:space:]]+' .env) (каждый прокси — ещё одна пара воркеров, профили proxy2..proxy4)"
fi

mem=$(awk '/MemAvailable/ {printf "%.1f", $2/1048576}' /proc/meminfo)
check "awk 'BEGIN{exit !($mem >= 3)}'" "свободная память: ${mem} ГБ" "нужно >= 3 ГБ на пару воркеров (обычно занимают ~1,2 ГБ)"
disk=$(df -Pk /var/lib/docker 2>/dev/null | awk 'NR==2 {printf "%.1f", $4/1048576}')
check "awk 'BEGIN{exit !(${disk:-0} >= 5)}'" "свободно на диске Docker: ${disk} ГБ" "под образ нужно ~2 ГБ"

if [ "$fail" -eq 0 ]; then
    echo; echo "Проверка внутри контейнера (собирает образ при первом запуске)"
    docker compose run --rm parser check || fail=$((fail + 1))
fi
echo
if [ "$fail" -eq 0 ]; then echo "Всё в порядке. Запуск: docker compose up -d kz kz-http"
else echo "Проблем: $fail — исправьте пункты FAIL и запустите проверку ещё раз"; fi
exit "$fail"
