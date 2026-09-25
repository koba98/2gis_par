"""
Резервный канал сбора данных через реальный браузер — используется, когда
API-ключ 2ГИС заблокирован (ApiKeyBlockedError) или Catalog/Reviews API
недоступны напрямую.

Техника (проверена пользователем на парсере остановок, dgis_stations_parser.py):
- Реальный системный Chrome через Playwright (channel="chrome") — обходит
  подмену SSL/антивирусные прокси корпоративной сети и заглушку "обновите
  браузер", которую голым aiohttp/requests не пройти (её обрабатывает JS
  сайта, museum.js).
- Заглушка "обновите браузер" обходится кликом по кнопке
  "Пропустить обновление браузера и перейти в 2ГИС" — как в
  dgis_stations_parser.bypass_browser_wall().

В отличие от парсера остановок (который читает `var initialState` из HTML),
здесь используется более надёжный приём: сайт 2gis.ru при отрисовке карточек
организаций и отзывов сам обращается к тем же публичным API
(catalog.api.2gis.com/3.0/items, public-api.reviews.2gis.com) собственным
встроенным ключом/сессией, не завязанным на ключ, который заблокировали нам.
Playwright перехватывает эти сетевые ответы (page.on("response")) — не нужно
гадать формат `initialState`, а JSON приходит в уже знакомой схеме, которую
разбирают parse_branch()/parse_review() из parser.py.

Технический момент (Windows): используется playwright.sync_api, а не
async_api. Причина — конфликт event loop'ов: async-соединение к PostgreSQL
(psycopg 3) на Windows работает только на SelectorEventLoop, а
playwright.async_api спавнит процесс браузера через asyncio-subprocess,
который на Windows поддерживает только ProactorEventLoop. Одновременно оба
в одном loop не работают (NotImplementedError на subprocess_exec).
Решение: синхронный Playwright выполняется в отдельном потоке с
единственным воркером (все вызовы должны идти из одного и того же
OS-потока — это требование самого Playwright sync API), а наружу отдаётся
асинхронный интерфейс через run_in_executor.

ВАЖНО: код написан по образцу проверенной техники, но сама выгрузка живыми
данными в среде разработки не проверялась — сеть, с которой запускался
скрипт при разработке, была заблокирована на уровне IP (2GIS Captcha /
403 Forbidden ещё до экрана "обновите браузер"). Проверяйте на своей
машине с обычным (не дата-центровым) IP — см. README, раздел "Ограничения".
"""

import asyncio
import logging
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import quote

logger = logging.getLogger(__name__)

try:
    from playwright.sync_api import Page, Response, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # playwright не установлен — фолбэк недоступен, но остальной парсер работает
    PLAYWRIGHT_AVAILABLE = False
    Page = Response = Any  # type: ignore

T = TypeVar("T")

# Те же viewport'ы, что в dgis_stations_parser.py — разные "отпечатки" контекста
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

COOLDOWN_MIN = 1.5
COOLDOWN_MAX = 2.6

# Домены API, ответы которых сайт получает сам при отрисовке страницы —
# именно их перехватывает снифер вместо прямых HTTP-запросов с нашим ключом.
CATALOG_RESPONSE_MARKERS = ("catalog.api.2gis.", "public-api.2gis.")
REVIEWS_RESPONSE_MARKERS = ("public-api.reviews.2gis.",)

# Slug города 2ГИС для URL вида https://2gis.kz/<slug>/search/... — используется
# при отсутствии готового slug у региона (см. REGION_SLUGS в parser.py).
DEFAULT_DOMAIN = "2gis.kz"


class CaptchaBlockedError(RuntimeError):
    """2ГИС показал reCAPTCHA/страницу 'Forbidden' (антибот-защита edge/CDN) —
    обойти автоматически нельзя, нужен другой IP/резидентный прокси."""


class BrowserWallError(RuntimeError):
    """Не удалось пройти заглушку 'обновите браузер' — устаревшая разметка
    сайта или изменился текст кнопки."""


def _looks_like_captcha(html: str) -> bool:
    return "g-recaptcha" in html or "2GIS Captcha" in html or "REQUEST-ID" in html


def _looks_like_browser_wall(html: str) -> bool:
    return "acceptRiskButton" in html


def _bypass_browser_wall(page: "Page") -> None:
    """Тот же обход, что в dgis_stations_parser.bypass_browser_wall()."""
    try:
        page.click("text=Пропустить обновление браузера и перейти в 2ГИС", timeout=5000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as exc:
        raise BrowserWallError(f"Не удалось обойти заглушку обновления браузера: {exc}") from exc


def _cooldown() -> None:
    import time
    time.sleep(random.uniform(COOLDOWN_MIN, COOLDOWN_MAX))


class _SyncBrowserWorker:
    """
    Владеет синхронными объектами Playwright/Chrome. Все методы должны
    вызываться из одного и того же потока-исполнителя (см. BrowserFallback,
    который прогоняет их через ThreadPoolExecutor(max_workers=1)).
    """

    def __init__(self, headless: bool, channel: Optional[str], locale: str,
                 domain: str, network_wait_ms: int):
        self.headless = headless
        self.channel = channel
        self.locale = locale
        self.domain = domain
        self.network_wait_ms = network_wait_ms
        self._pw = None
        self._browser = None

    def start(self) -> None:
        self._pw = sync_playwright().start()
        launch_kwargs: Dict[str, Any] = {"headless": self.headless}
        if self.channel and self.channel != "chromium":
            launch_kwargs["channel"] = self.channel
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        logger.info("Браузерный фолбэк запущен (channel=%s, headless=%s).",
                    self.channel, self.headless)

    def stop(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None
        logger.info("Браузерный фолбэк остановлен.")

    def _new_page(self) -> "Page":
        context = self._browser.new_context(
            viewport=random.choice(VIEWPORTS), locale=self.locale,
        )
        return context.new_page()

    def _goto_and_sniff(
        self, url: str, response_markers: tuple, post_actions=None,
    ) -> List[Dict[str, Any]]:
        """
        Открывает страницу и собирает JSON-тела всех сетевых ответов,
        URL которых содержит любой из response_markers. Опционально
        выполняет post_actions(page) после загрузки (например, скролл
        для подгрузки следующих страниц отзывов).
        """
        page = self._new_page()
        captured: List[Dict[str, Any]] = []

        def on_response(response: "Response") -> None:
            resp_url = response.url
            if not any(marker in resp_url for marker in response_markers):
                return
            try:
                if response.status != 200:
                    return
                captured.append(response.json())
            except Exception:
                # Не все совпавшие ответы — JSON (могут быть картинки CDN и т.п.)
                pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            html = page.content()
            if _looks_like_captcha(html):
                raise CaptchaBlockedError(
                    f"2ГИС показал антибот-проверку (captcha/forbidden) для {url}. "
                    "IP этой сети заблокирован — нужен другой (не дата-центровый) "
                    "IP или резидентный прокси."
                )
            if _looks_like_browser_wall(html):
                logger.warning("Поймали заглушку 'обновите браузер' — обходим кликом.")
                _bypass_browser_wall(page)

            # Даём JS время сделать XHR-запросы к API и отрисовать карточки
            page.wait_for_timeout(self.network_wait_ms)

            if post_actions:
                post_actions(page)
                page.wait_for_timeout(self.network_wait_ms)

        finally:
            page.context.close()

        return captured

    def search_organizations(self, query: str, city_slug: str, page_num: int) -> List[Dict[str, Any]]:
        path = f"/{city_slug}/search/{quote(query)}"
        if page_num > 1:
            path += f"/page/{page_num}"
        url = f"https://{self.domain}{path}"

        logger.info("Браузерный фолбэк: поиск '%s' в %s, страница %d", query, city_slug, page_num)
        payloads = self._goto_and_sniff(url, CATALOG_RESPONSE_MARKERS)
        _cooldown()

        items: List[Dict[str, Any]] = []
        for payload in payloads:
            result = (payload or {}).get("result") or {}
            page_items = result.get("items") or []
            if page_items:
                items.extend(page_items)

        logger.info("Браузерный фолбэк: перехвачено %d карточек организаций.", len(items))
        return items

    def fetch_reviews(self, branch_id: int, city_slug: str) -> List[Dict[str, Any]]:
        url = f"https://{self.domain}/{city_slug}/firm/{branch_id}/tab/reviews"
        logger.info("Браузерный фолбэк: отзывы филиала %s (%s)", branch_id, city_slug)

        def scroll_to_load_more(page: "Page") -> None:
            for _ in range(15):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(700)
                try:
                    btn = page.get_by_text("Показать ещё", exact=False)
                    if btn.count() > 0:
                        btn.first.click(timeout=2000)
                        page.wait_for_timeout(700)
                except Exception:
                    pass

        payloads = self._goto_and_sniff(
            url, REVIEWS_RESPONSE_MARKERS, post_actions=scroll_to_load_more
        )
        _cooldown()

        reviews: List[Dict[str, Any]] = []
        seen_ids = set()
        for payload in payloads:
            for r in payload.get("reviews") or []:
                rid = r.get("id")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    reviews.append(r)

        logger.info(
            "Браузерный фолбэк: перехвачено %d уникальных отзывов для филиала %s.",
            len(reviews), branch_id,
        )
        return reviews


class BrowserFallback:
    """
    Асинхронная обёртка над синхронным Playwright, выполняемым в одном
    выделенном потоке (требование Playwright sync API — все вызовы из
    одного и того же OS-потока). Используется как контекстный менеджер:

        async with BrowserFallback() as fb:
            items = await fb.search_organizations("кофейня", "almaty")
            reviews = await fb.fetch_reviews(branch_id, "almaty")
    """

    def __init__(
        self,
        headless: bool = True,
        channel: Optional[str] = "chrome",
        locale: str = "ru-RU",
        domain: str = DEFAULT_DOMAIN,
        network_wait_ms: int = 3500,
    ):
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright не установлен. Выполните: pip install playwright "
                "(системный Chrome уже используется через channel='chrome', "
                "'playwright install' не требуется)."
            )
        self._worker = _SyncBrowserWorker(headless, channel, locale, domain, network_wait_ms)
        # Один поток на весь фолбэк — Playwright sync API требует, чтобы все
        # вызовы шли из того же потока, где был создан браузер.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="2gis-browser")

    async def _run(self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    async def start(self) -> "BrowserFallback":
        await self._run(self._worker.start)
        return self

    async def close(self) -> None:
        try:
            await self._run(self._worker.stop)
        finally:
            self._executor.shutdown(wait=True)

    async def __aenter__(self) -> "BrowserFallback":
        return await self.start()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def search_organizations(
        self, query: str, city_slug: str, page_num: int = 1
    ) -> List[Dict[str, Any]]:
        return await self._run(lambda: self._worker.search_organizations(query, city_slug, page_num))

    async def fetch_reviews(self, branch_id: int, city_slug: str) -> List[Dict[str, Any]]:
        return await self._run(lambda: self._worker.fetch_reviews(branch_id, city_slug))
