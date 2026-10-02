"""
HTTP-запросы к 2gis.kz без браузера.

Сервер 2ГИС отдаёт страницы карточки, «В здании», маршрута, остановки, рубрикатора и первую
страницу выдачи уже с данными (initialState в HTML) обычному HTTP-клиенту — без кук и без
рендера (проверено). Браузер нужен только для листания выдачи кликами, длинных списков
«Загрузить ещё» и как запасной путь, если HTTP-ответ — капча или заглушка.

Все потоки делят один адаптивный ограничитель частоты: пока 2ГИС отвечает нормально, интервал
между запросами постепенно сокращается до нижней границы; при капче, 403 или 429 он удваивается
и делается пауза. Так параллельные запросы прячут сетевую задержку, а общий темп с IP остаётся
под контролем.
"""

import itertools
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("dgis_fetcher")

SITE = "https://2gis.kz"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
HTML_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
}
API_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": f"{SITE}/", "Origin": SITE}

BASE_INTERVAL = 1.0        # с, интервал между запросами на старте (≈1 запрос/с со всех потоков)
MIN_INTERVAL = 0.5         # с, быстрее не разгоняться
MAX_INTERVAL = 15.0        # с, медленнее после череды блокировок
SPEEDUP_EVERY = 200        # после стольких успешных запросов подряд — интервал −10%
BLOCK_COOLDOWN = 60.0      # с, пауза всех потоков после капчи/403/429
LONG_BREAK_EVERY = 500     # каждые N запросов — перерыв, как у браузера
LONG_BREAK = (20.0, 40.0)


def looks_like_captcha(html: str) -> bool:
    return "g-recaptcha" in html or "2GIS Captcha" in html


class Blocked(Exception):
    """2ГИС ответил капчей, 403 или 429: ограничитель уже замедлился, запрос стоит повторить в браузере."""


class RateLimiter:
    """Общий для всех потоков темп запросов с разбросом ±30% и адаптацией к ответам 2ГИС."""

    def __init__(self, pace_factor: float = 1.0):
        self.pace = pace_factor
        self.interval = BASE_INTERVAL * pace_factor
        self._lock = threading.Lock()
        self._next = 0.0
        self._count = 0
        self._ok_streak = 0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._count += 1
            start = max(now, self._next)
            gap = self.interval * random.uniform(0.7, 1.3)
            if self._count % LONG_BREAK_EVERY == 0:
                pause = random.uniform(*LONG_BREAK) * self.pace
                logger.info("Антибан (HTTP): перерыв %.0f сек после %d запросов", pause, self._count)
                gap += pause
            self._next = start + gap
        if start > now:
            time.sleep(start - now)

    def success(self) -> None:
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= SPEEDUP_EVERY:
                self._ok_streak = 0
                self.interval = max(MIN_INTERVAL * self.pace, self.interval * 0.9)

    def blocked(self) -> None:
        with self._lock:
            self._ok_streak = 0
            self.interval = min(MAX_INTERVAL * self.pace, self.interval * 2)
            self._next = max(self._next, time.monotonic() + BLOCK_COOLDOWN * self.pace)
            logger.warning("2ГИС ограничивает запросы — замедляемся: интервал %.1f с, пауза %.0f с",
                           self.interval, BLOCK_COOLDOWN * self.pace)


class HttpFetcher:
    """
    Потокобезопасный HTTP-клиент (httpx): по клиенту на прокси, запросы по кругу.
    concurrency — сколько запросов держать в полёте параллельно (размер пула потоков краулера).
    """

    def __init__(self, proxies: Optional[List[str]] = None, pace_factor: float = 1.0, concurrency: int = 3):
        self.concurrency = max(1, concurrency)
        self.limiter = RateLimiter(pace_factor)
        limits = httpx.Limits(max_connections=self.concurrency + 2, max_keepalive_connections=self.concurrency + 2)
        self._clients = [
            httpx.Client(headers=HTML_HEADERS, proxy=p or None, timeout=30.0, follow_redirects=True, limits=limits)
            for p in (proxies or [None])
        ]
        self._next_client = itertools.count()

    def close(self) -> None:
        for c in self._clients:
            c.close()

    def _client(self) -> httpx.Client:
        return self._clients[next(self._next_client) % len(self._clients)]

    def get_html(self, url: str) -> Tuple[Optional[str], str]:
        """
        (HTML, итоговый URL после редиректов). HTML None — сетевой сбой или страница без данных
        (заглушка): её стоит открыть в браузере. Капча/403/429 — исключение Blocked.
        """
        for attempt in range(2):
            self.limiter.wait()
            try:
                resp = self._client().get(url)
            except httpx.HTTPError as e:
                logger.debug("HTTP %s: %s (попытка %d)", url, e, attempt + 1)
                time.sleep(2)
                continue
            html = resp.text
            if resp.status_code in (403, 429) or looks_like_captcha(html):
                self.limiter.blocked()
                raise Blocked(f"{resp.status_code} {url}")
            if resp.status_code == 404:
                self.limiter.success()
                return html, str(resp.url)  # объекта больше нет
            if resp.status_code == 200 and "var initialState" in html:
                self.limiter.success()
                return html, str(resp.url)
            logger.debug("HTTP %s: статус %s без данных страницы — нужен браузер", url, resp.status_code)
            return None, str(resp.url)
        return None, url

    def get_json(self, url: str) -> Optional[Dict[str, Any]]:
        """JSON из API, к которому обращается сам сайт (лента отзывов, комментарии). None — не удалось."""
        for attempt in range(3):
            self.limiter.wait()
            try:
                resp = self._client().get(url, headers=API_HEADERS)
            except httpx.HTTPError as e:
                logger.debug("API %s: %s (попытка %d)", url, e, attempt + 1)
                time.sleep(2)
                continue
            if resp.status_code == 200:
                self.limiter.success()
                try:
                    return resp.json()
                except ValueError:
                    return None
            if resp.status_code in (403, 429):
                self.limiter.blocked()  # следующая попытка подождёт паузу ограничителя
                continue
            logger.debug("API %s: статус %s", url, resp.status_code)
            return None
        return None
