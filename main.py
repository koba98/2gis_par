"""
Главная точка входа парсера 2ГИС.

Подкоманды:
    init-db   создать схему и таблицы в PostgreSQL
    crawl     полный обход региона (все рубрики или заданные запросы) + отзывы
    reviews   (до)собрать отзывы по уже сохранённым филиалам
    stats     показать количество записей в БД

Настройки подключения и ключи можно задать в .env (см. .env.example).
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from client import AntiBanHttpClient, ApiKeyBlockedError
from parser import DEFAULT_CATALOG_KEY, DEFAULT_REVIEWS_KEY, TwoGisCrawler
from storage import Storage


def load_dotenv(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    """Подгружает переменные из .env (без перезаписи уже заданных в окружении)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def setup_logging(verbose: bool = False) -> None:
    """Настраивает форматированный вывод логов в консоль."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def load_proxies_from_file(file_path: Optional[str]) -> List[str]:
    """Загружает список прокси из текстового файла (по одному на строку)."""
    if not file_path:
        return []
    if not os.path.exists(file_path):
        logging.warning("Файл прокси '%s' не найден.", file_path)
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        proxies = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    logging.info("Загружено %d прокси-серверов из файла %s", len(proxies), file_path)
    return proxies


def parse_bbox(value: str):
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox задаётся как min_lon,min_lat,max_lon,max_lat")
    return tuple(parts)


async def log_stats(storage: Storage) -> None:
    stats = await storage.get_stats()
    logging.info("=" * 60)
    logging.info("В БД (схема %s): %s", storage.schema,
                 ", ".join(f"{k}={v}" for k, v in stats.items()))
    logging.info("=" * 60)


# Категории для браузерного фолбэка каталога, когда рубрикатор недоступен
# (сам API заблокирован) и пользователь не передал -q явно. Покрытие хуже,
# чем через рубрикатор, но позволяет собирать каталог вообще без ключа.
DEFAULT_BROWSER_QUERIES = [
    "кафе", "ресторан", "фастфуд", "магазин продукты", "супермаркет",
    "аптека", "салон красоты", "парикмахерская", "автосервис", "автозапчасти",
    "стоматология", "клиника", "больница", "банк", "отель", "фитнес клуб",
    "школа", "детский сад", "юридические услуги", "недвижимость", "одежда",
    "строительный магазин", "автосалон", "ветеринарная клиника", "нотариус",
]

class FallbackHolder:
    """Лениво создаёт и переиспользует один BrowserFallback на весь прогон
    (запуск реального Chrome — дорогая операция, не на каждый регион)."""

    def __init__(self, headless: bool):
        self.headless = headless
        self._fallback = None

    async def get(self):
        if self._fallback is None:
            from web_fallback import BrowserFallback  # опциональная зависимость (playwright)
            self._fallback = await BrowserFallback(headless=self.headless).start()
        return self._fallback

    async def close(self) -> None:
        if self._fallback is not None:
            await self._fallback.close()
            self._fallback = None


async def crawl_region(
    crawler: TwoGisCrawler,
    storage: Storage,
    region: dict,
    args: argparse.Namespace,
    fallback_holder: "FallbackHolder",
) -> None:
    """Обходит один регион: каталог + (опционально) отзывы, с автоматическим
    переключением на браузерный фолбэк при ApiKeyBlockedError."""
    bbox = args.bbox or (region["min_lon"], region["min_lat"], region["max_lon"], region["max_lat"])
    if None in bbox:
        logging.warning("Пропуск региона %s: отсутствуют координаты bbox.", region["name"])
        return
    if args.fresh:
        logging.info("Удалено задач прошлого обхода: %d", await storage.clear_tasks(region["id"]))

    try:
        await crawler.seed_tasks(region, bbox, queries=args.query, rubric_filter=args.rubric)
        await crawler.crawl_catalog(region["id"], workers=args.workers)
    except ApiKeyBlockedError as exc:
        if not args.browser_fallback:
            raise SystemExit(
                f"Ключ каталога заблокирован ({exc}). Запустите с --browser-fallback, "
                "чтобы дособрать данные напрямую через браузер."
            )
        logging.warning("Ключ каталога заблокирован (%s). Переключаемся на браузерный фолбэк.", exc)
        slug = crawler.region_slug(region["name"])
        if not slug:
            logging.error(
                "Нет slug для региона '%s' в REGION_SLUGS (parser.py) — "
                "браузерный фолбэк для каталога невозможен, пропускаю.", region["name"]
            )
        else:
            terms = (
                args.query
                or await storage.crawl_task_search_terms(region["id"])
                or DEFAULT_BROWSER_QUERIES
            )
            fb = await fallback_holder.get()
            await crawler.crawl_catalog_via_browser(
                fb, region["id"], slug, terms, max_pages=args.browser_max_pages
            )

    if args.no_reviews:
        return
    try:
        await crawler.crawl_reviews(region["id"], workers=args.workers,
                                    only_missing=getattr(args, "only_missing", False))
    except ApiKeyBlockedError as exc:
        if not args.browser_fallback:
            raise SystemExit(
                f"Ключ отзывов заблокирован ({exc}). Запустите с --browser-fallback, "
                "чтобы дособрать отзывы напрямую через браузер."
            )
        logging.warning("Ключ отзывов заблокирован (%s). Переключаемся на браузерный фолбэк.", exc)
        slug = crawler.region_slug(region["name"])
        if not slug:
            logging.error(
                "Нет slug для региона '%s' в REGION_SLUGS (parser.py) — "
                "браузерный фолбэк для отзывов невозможен, пропускаю.", region["name"]
            )
        else:
            fb = await fallback_holder.get()
            await crawler.crawl_reviews_via_browser(
                fb, region["id"], slug, only_missing=getattr(args, "only_missing", False)
            )


async def run(args: argparse.Namespace) -> None:
    if not args.dsn:
        raise SystemExit("Не задана строка подключения: --dsn или переменная PG_DSN.")

    fallback_holder = FallbackHolder(headless=not args.browser_show)

    async with Storage(args.dsn, args.schema) as storage:
        if args.command == "init-db":
            await storage.init_db()
            await log_stats(storage)
            return
        if args.command == "stats":
            await log_stats(storage)
            logging.info("Очередь обхода: %s", await storage.task_stats())
            return

        client = AntiBanHttpClient(
            proxies=load_proxies_from_file(args.proxies),
            base_delay=args.delay,
            max_delay=args.max_delay,
            request_timeout=args.timeout,
        )
        try:
            async with client:
                crawler = TwoGisCrawler(
                    client=client,
                    storage=storage,
                    catalog_key=args.catalog_key,
                    reviews_key=args.reviews_key,
                    page_size=getattr(args, "page_size", 10),
                    max_pages=getattr(args, "max_pages", 5),
                    min_tile_deg=getattr(args, "min_tile", 0.002),
                )

                if args.command == "crawl":
                    await storage.init_db()
                    if args.country:
                        regions = await crawler.list_country_regions(country_code=args.country)
                        logging.info("Найдено %d регионов для страны '%s'", len(regions), args.country)
                        for reg in regions:
                            logging.info("=" * 60)
                            logging.info(">>> ОБХОД РЕГИОНА: %s (ID %s)", reg["name"], reg["id"])
                            logging.info("=" * 60)
                            await crawl_region(crawler, storage, reg, args, fallback_holder)
                    else:
                        region = await crawler.resolve_region(name=args.region, region_id=args.region_id)
                        await crawl_region(crawler, storage, region, args, fallback_holder)

                elif args.command == "reviews":
                    if args.country:
                        regions = await crawler.list_country_regions(country_code=args.country)
                        for reg in regions:
                            logging.info(">>> СБОР ОТЗЫВОВ РЕГИОНА: %s (ID %s)", reg["name"], reg["id"])
                            try:
                                await crawler.crawl_reviews(reg["id"], workers=args.workers,
                                                            only_missing=args.only_missing)
                            except ApiKeyBlockedError as exc:
                                if not args.browser_fallback:
                                    raise SystemExit(
                                        f"Ключ отзывов заблокирован ({exc}). "
                                        "Запустите с --browser-fallback."
                                    )
                                slug = crawler.region_slug(reg["name"])
                                if slug:
                                    fb = await fallback_holder.get()
                                    await crawler.crawl_reviews_via_browser(
                                        fb, reg["id"], slug, only_missing=args.only_missing
                                    )
                    elif args.region:
                        region = await crawler.resolve_region(name=args.region)
                        await crawler.crawl_reviews(region["id"], workers=args.workers,
                                                    only_missing=args.only_missing)
                    else:
                        await crawler.crawl_reviews(args.region_id, workers=args.workers,
                                                    only_missing=args.only_missing)
            await log_stats(storage)
        finally:
            await fallback_holder.close()


def parse_arguments() -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn", default=os.getenv("PG_DSN"),
                        help="Строка подключения PostgreSQL (env PG_DSN), "
                             "например postgresql://user:pass@localhost:5432/db")
    common.add_argument("--schema", default=os.getenv("PG_SCHEMA", "twogis"),
                        help="Схема для таблиц (env PG_SCHEMA). По умолчанию: twogis")
    common.add_argument("--catalog-key", default=os.getenv("TWOGIS_CATALOG_KEY", DEFAULT_CATALOG_KEY),
                        help="Ключ Catalog API 2ГИС (env TWOGIS_CATALOG_KEY)")
    common.add_argument("--reviews-key", default=os.getenv("TWOGIS_REVIEWS_KEY", DEFAULT_REVIEWS_KEY),
                        help="Ключ Reviews API 2ГИС (env TWOGIS_REVIEWS_KEY)")
    common.add_argument("--proxies", default=None,
                        help="Файл со списком прокси (http://ip:port или http://user:pass@ip:port)")
    common.add_argument("--delay", type=float, default=1.0,
                        help="Базовая пауза между запросами, сек. По умолчанию: 1.0")
    common.add_argument("--max-delay", type=float, default=30.0,
                        help="Максимальный бэкофф при 429/403, сек. По умолчанию: 30.0")
    common.add_argument("--timeout", type=float, default=15.0,
                        help="Таймаут HTTP-запроса, сек. По умолчанию: 15.0")
    common.add_argument("--workers", type=int, default=2,
                        help="Число параллельных воркеров (частоту ограничивает --delay). По умолчанию: 2")
    common.add_argument("-v", "--verbose", action="store_true", help="DEBUG-логирование")
    common.add_argument("--browser-fallback", action="store_true",
                        help="При блокировке API-ключа переключаться на прямой сбор "
                             "через реальный браузер (Playwright + системный Chrome), "
                             "как в dgis_stations_parser.py. Требует: pip install playwright")
    common.add_argument("--browser-show", action="store_true",
                        help="Показывать окно браузера (по умолчанию headless)")
    common.add_argument("--browser-max-pages", type=int, default=50,
                        help="Максимум страниц поиска на сайте на один запрос при "
                             "браузерном фолбэке. По умолчанию: 50")

    parser = argparse.ArgumentParser(
        description="Полный парсер каталога и отзывов 2ГИС с сохранением в PostgreSQL."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", parents=[common], help="Создать схему и таблицы")
    sub.add_parser("stats", parents=[common], help="Статистика по БД")

    crawl = sub.add_parser("crawl", parents=[common],
                           help="Полный обход региона: все организации и отзывы")
    target = crawl.add_mutually_exclusive_group(required=True)
    target.add_argument("-r", "--region", help="Название региона/города, например 'Алматы'")
    target.add_argument("--region-id", type=int, help="ID региона 2ГИС")
    target.add_argument("--country", help="Код страны для обхода всех регионов (например 'kz' для Казахстана)")
    crawl.add_argument("--bbox", type=parse_bbox, default=None,
                       help="Ограничить область: min_lon,min_lat,max_lon,max_lat")
    crawl.add_argument("-q", "--query", action="append",
                       help="Поисковый запрос вместо обхода рубрикатора (можно несколько раз)")
    crawl.add_argument("--rubric", action="append",
                       help="Обходить только рубрики, содержащие подстроку (можно несколько раз)")
    crawl.add_argument("--page-size", type=int, default=10,
                       help="Размер страницы выдачи (демо-ключ: до 10, коммерческий: до 50)")
    crawl.add_argument("--max-pages", type=int, default=5,
                       help="Сколько страниц API позволяет пролистать (демо-ключ: 5)")
    crawl.add_argument("--min-tile", type=float, default=0.002,
                       help="Минимальный размер тайла в градусах. По умолчанию: 0.002 (~200 м)")
    crawl.add_argument("--no-reviews", action="store_true", help="Не собирать отзывы")
    crawl.add_argument("--fresh", action="store_true",
                       help="Начать обход заново (сбросить очередь тайлов региона)")

    reviews = sub.add_parser("reviews", parents=[common],
                             help="Собрать отзывы по сохранённым филиалам")
    reviews.add_argument("--region-id", type=int, default=None, help="Только филиалы региона")
    reviews.add_argument("-r", "--region", help="Название региона/города")
    reviews.add_argument("--country", help="Код страны для сбора отзывов по всем регионам (например 'kz')")
    reviews.add_argument("--only-missing", action="store_true",
                         help="Только филиалы, по которым отзывы ещё не собирались")

    return parser.parse_args()


def main():
    """Точка входа CLI."""
    load_dotenv()
    args = parse_arguments()
    setup_logging(args.verbose)

    try:
        # psycopg в async-режиме не работает с ProactorEventLoop (Windows)
        asyncio.run(run(args), loop_factory=asyncio.SelectorEventLoop)
    except KeyboardInterrupt:
        logging.info("Работа прервана (Ctrl+C). Незавершённые задачи продолжатся при следующем запуске.")


if __name__ == "__main__":
    main()
