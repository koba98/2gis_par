#!/bin/sh
# Виртуальный дисплей для Chrome «с окном» (headless 2ГИС чаще встречает капчей), затем парсер.
# xvfb-run здесь не годится: в контейнере он зависал навсегда, не запустив команду.
set -e
DISPLAY_NUM=99
rm -f /tmp/.X${DISPLAY_NUM}-lock /tmp/.X11-unix/X${DISPLAY_NUM}  # остатки после перезапуска контейнера
Xvfb :${DISPLAY_NUM} -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:${DISPLAY_NUM}
i=0
until [ -e /tmp/.X11-unix/X${DISPLAY_NUM} ]; do
    i=$((i + 1))
    if [ "$i" -gt 100 ]; then echo "Xvfb не запустился за 10 с" >&2; exit 1; fi
    sleep 0.1
done
exec python main.py "$@"
