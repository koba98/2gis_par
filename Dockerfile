# Парсер 2ГИС: Python + системный Google Chrome + Xvfb.
# 2ГИС чаще показывает капчу headless-браузеру, поэтому Chrome работает «с окном»
# на виртуальном дисплее Xvfb — видеокарта и монитор на сервере не нужны.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

# wget не удалять: от него зависит пакет google-chrome-stable (purge wget удалял и сам Chrome)
RUN apt-get update \
 && apt-get install -y --no-install-recommends wget gnupg ca-certificates xvfb xauth fonts-dejavu-core \
 && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && test -x /opt/google/chrome/chrome \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./
COPY sql ./sql

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN tr -d '\r' < /usr/local/bin/docker-entrypoint.sh > /tmp/ep \
 && mv /tmp/ep /usr/local/bin/docker-entrypoint.sh \
 && chmod +x /usr/local/bin/docker-entrypoint.sh

# виртуальный дисплей Xvfb + python main.py <команда>
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["stats"]
