"""
Сетевой модуль с системой защиты от блокировок (Anti-Ban).
Реализует ротацию User-Agent, пула прокси, экспоненциальный бэкофф при 429/403
и адаптивный троттлинг запросов к серверам 2ГИС.
"""

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional
import aiohttp

logger = logging.getLogger(__name__)

# Пул актуальных браузерных User-Agent для десктопов
USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Firefox on Windows & macOS
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]


class ProxyManager:
    """Менеджер пула прокси-серверов с отслеживанием ошибок и ротацией."""

    def __init__(self, proxies: Optional[List[str]] = None, max_fails: int = 3):
        self._raw_proxies = [p.strip() for p in (proxies or []) if p.strip()]
        # Словарь прокси -> количество ошибок
        self._proxy_stats: Dict[str, int] = {p: 0 for p in self._raw_proxies}
        self._active_proxies: List[str] = list(self._raw_proxies)
        self._max_fails = max_fails
        self._index = 0

    def get_proxy(self) -> Optional[str]:
        """Возвращает следующий прокси по принципу Round-Robin."""
        if not self._active_proxies:
            # Если все прокси временно заблокированы, сбрасываем штрафы
            if self._raw_proxies:
                logger.warning("Все прокси получили ошибки. Сброс штрафов прокси.")
                self._proxy_stats = {p: 0 for p in self._raw_proxies}
                self._active_proxies = list(self._raw_proxies)
            else:
                return None

        proxy = self._active_proxies[self._index % len(self._active_proxies)]
        self._index += 1
        return proxy

    def report_failure(self, proxy: Optional[str]) -> None:
        """Регистрирует сбой прокси. При превышении порога ошибок исключает его из активных."""
        if not proxy or proxy not in self._proxy_stats:
            return

        self._proxy_stats[proxy] += 1
        fails = self._proxy_stats[proxy]
        logger.warning("Прокси %s получил сбой (%d/%d).", proxy, fails, self._max_fails)

        if fails >= self._max_fails and proxy in self._active_proxies:
            self._active_proxies.remove(proxy)
            logger.error("Прокси %s исключен из ротации из-за частых ошибок.", proxy)

    def report_success(self, proxy: Optional[str]) -> None:
        """Сбрасывает счетчик ошибок при успешном запросе."""
        if proxy and proxy in self._proxy_stats:
            self._proxy_stats[proxy] = max(0, self._proxy_stats[proxy] - 1)

    @property
    def total_count(self) -> int:
        return len(self._raw_proxies)

    @property
    def active_count(self) -> int:
        return len(self._active_proxies)


class AntiBanHttpClient:
    """
    Асинхронный HTTP-клиент с защитой от блокировок:
    - Ротация User-Agent
    - Ротация прокси
    - Экспоненциальный бэкофф (при 429 Too Many Requests и 403 Forbidden)
    - Умный троттлинг с рандомизированной паузой (jitter)
    """

    def __init__(
        self,
        proxies: Optional[List[str]] = None,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        max_retries: int = 5,
        request_timeout: float = 15.0,
    ):
        self.proxy_manager = ProxyManager(proxies)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.max_retries = max_retries
        self.timeout = aiohttp.ClientTimeout(total=request_timeout)

        # Текущая пауза троттлинга (увеличивается при блокировках)
        self._current_backoff = base_delay
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        # Глобальный троттлинг: общий интервал между запросами для всех воркеров
        self._throttle_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def get_session(self) -> aiohttp.ClientSession:
        """Получает или создает активную клиентскую aiohttp-сессию."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        """Корректно закрывает активную сессию aiohttp."""
        if self._session and not self._session.closed:
            await self._session.close()
            # Короткая пауза для закрытия SSL-транспортов в asyncio
            await asyncio.sleep(0.05)
            self._session = None
            logger.info("HTTP-сессия успешно закрыта.")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _get_random_user_agent(self) -> str:
        """Выбирает случайный User-Agent из браузерного пула."""
        return random.choice(USER_AGENTS)

    def _build_headers(self, custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Генерирует заголовки, имитирующие реальный браузер."""
        headers = {
            "User-Agent": self._get_random_user_agent(),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://2gis.ru/",
            "Origin": "https://2gis.ru",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        if custom_headers:
            headers.update(custom_headers)
        return headers

    async def _throttle(self) -> None:
        """
        Выдерживает адаптивную паузу со случайным шумом (jitter) между запросами.
        Интервал общий для всех конкурентных воркеров, использующих клиент.
        """
        loop = asyncio.get_running_loop()
        async with self._throttle_lock:
            wait = self._next_request_at - loop.time()
            if wait > 0:
                logger.debug("Троттлинг: пауза %.2f сек...", wait)
                await asyncio.sleep(wait)
            # Рандомизация паузы: +/- 25% от текущего значения
            jitter = random.uniform(0.75, 1.25)
            self._next_request_at = loop.time() + self._current_backoff * jitter

    async def request_json(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        method: str = "GET",
    ) -> Dict[str, Any]:
        """
        Выполняет HTTP-запрос с защитой от блокировок, ротацией прокси и бэкоффом.
        Возвращает распарсенный JSON-ответ.
        """
        session = await self.get_session()

        for attempt in range(1, self.max_retries + 1):
            await self._throttle()

            current_proxy = self.proxy_manager.get_proxy()
            request_headers = self._build_headers(headers)

            try:
                logger.debug(
                    "Запрос [попытка %d/%d] URL: %s (Прокси: %s)",
                    attempt,
                    self.max_retries,
                    url,
                    current_proxy or "direct",
                )

                async with session.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=request_headers,
                    proxy=current_proxy,
                ) as response:

                    status = response.status

                    # Успешный ответ
                    if status == 200:
                        self.proxy_manager.report_success(current_proxy)
                        # Постепенное восстановление базовой задержки при успехе
                        self._current_backoff = max(
                            self.base_delay,
                            self._current_backoff * 0.9,
                        )
                        return await response.json()

                    # Обработка анти-фрод кодов 429 (Too Many Requests) и 403 (Forbidden)
                    if status in (429, 403):
                        self.proxy_manager.report_failure(current_proxy)
                        # Экспоненциальное увеличение задержки бэкоффа
                        async with self._lock:
                            self._current_backoff = min(
                                self.max_delay,
                                max(self.base_delay * 2, self._current_backoff * self.backoff_factor),
                            )
                        logger.warning(
                            "Получен статус %d от 2ГИС. Активирован экспоненциальный бэкофф! "
                            "Новая задержка: %.2f сек (попытка %d/%d)",
                            status,
                            self._current_backoff,
                            attempt,
                            self.max_retries,
                        )
                        continue

                    # Прочие ошибки сервера (5xx)
                    if 500 <= status < 600:
                        logger.warning(
                            "Сервер 2ГИС вернул ошибку %d. Повторная попытка...",
                            status,
                        )
                        continue

                    # Неожиданный клиентский статус (400, 404 и т.д.)
                    error_text = await response.text()
                    logger.error(
                        "Запрос завершился со статусом %d: %s",
                        status,
                        error_text[:200],
                    )
                    # Проверяем, возможно в теле ответа есть json с кодом ошибки
                    try:
                        return await response.json()
                    except Exception:
                        response.raise_for_status()

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.proxy_manager.report_failure(current_proxy)
                logger.warning(
                    "Сетевая ошибка при запросе (%s): %s. Попытка %d/%d",
                    type(exc).__name__,
                    exc,
                    attempt,
                    self.max_retries,
                )
                # Увеличиваем паузу при сетевых сбоях
                self._current_backoff = min(
                    self.max_delay,
                    self._current_backoff * 1.5,
                )

        raise RuntimeError(
            f"Не удалось выполнить запрос к {url} после {self.max_retries} попыток."
        )
