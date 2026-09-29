"""
CLI браузерного парсера 2ГИС.

Подкоманды:
    init-db     создать схему, таблицы, индексы и подписи в PostgreSQL
    area        все объекты одного района: скан карты, «В здании», полные карточки, отзывы
    catalog     все объекты города: рубрики, площадки, ЖК, организации внутри зданий
    cards       полные карточки (телефоны, соцсети, email, атрибуты) для объектов без них
    transport   остановки и маршруты (автобус, метро, троллейбус, трамвай, маршрутка)
    crawl       transport + catalog
    reviews     отзывы и комментарии к ним для объектов, уже сохранённых в БД
    stats       объём данных, разбивка по категориям и полнота обхода

Подключение к БД — в .env (PG_DSN, PG_SCHEMA).
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from parser import REGIONS, TRANSPORT_SUBTYPES, City, DgisBrowserScraper, DgisCrawler
from storage import SyncStorage

logger = logging.getLogger("dgis_main")


def load_dotenv(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    """Переменные из .env (уже заданные в окружении не перезаписываются)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def resolve_cities(region: Optional[str], cities: Optional[str]) -> List[City]:
    """-r 'Астана' / --cities 'Астана,Алматы' / --cities all -> список городов."""
    if cities and cities.strip().lower() == "all":
        names = list(REGIONS)
    else:
        names = [region] if region else [c.strip() for c in (cities or "").split(",") if c.strip()]
    if not names:
        raise SystemExit("Укажите город: -r 'Астана' или --cities 'Астана,Алматы' или --cities all")

    result: Dict[str, City] = {}
    for name in names:
        match = next(
            (City(reg, city, slug) for reg, (city, slug) in REGIONS.items()
             if name.lower() in (reg.lower(), city.lower(), slug)),
            None,
        )
        if match is None:
            raise SystemExit(f"Неизвестный город/регион: {name}. Доступны: {', '.join(REGIONS)}")
        result[match.slug] = match
    return list(result.values())


def load_proxies(path: Optional[str], single: Optional[str]) -> List[str]:
    proxies = []
    if path:
        proxies = [
            line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if single:
        proxies.append(single)
    return proxies


def print_stats(storage: SyncStorage, city: Optional[City]) -> None:
    logger.info("=" * 70)
    for k, v in storage.get_stats().items():
        logger.info("  %-24s %d", k, v)
    cats = storage.get_category_stats(city.name if city else None)
    if cats:
        logger.info("Категории%s (топ-50):", f" ({city.name})" if city else "")
        for row in cats:
            logger.info("  %-40s %d", row["category"], row["count"])
    tasks = storage.web_task_stats(city.slug if city else None)
    if tasks:
        logger.info("Полнота обхода (заявлено 2ГИС / собрано):")
        for row in tasks:
            logger.info("  %-10s %-9s %-11s задач %-6d %d / %d",
                        row["city_slug"], row["kind"], row["status"], row["tasks"],
                        row["total"], row["collected"])
    logger.info("=" * 70)


def parse_shard(value: str) -> Tuple[int, int]:
    i, n = (int(x) for x in value.split("/"))
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError("--shard ожидает i/N, где 0 <= i < N, например 0/3")
    return i, n


def parse_arguments() -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn", default=os.getenv("PG_DSN"), help="PostgreSQL (env PG_DSN)")
    common.add_argument("--schema", default=os.getenv("PG_SCHEMA", "twogis"), help="схема (env PG_SCHEMA)")
    common.add_argument("-v", "--verbose", action="store_true", help="DEBUG-логи")

    browser = argparse.ArgumentParser(add_help=False)
    browser.add_argument("-r", "--region", help="город/регион, например 'Астана'")
    browser.add_argument("--cities", help="'all' или список через запятую")
    browser.add_argument("--headless", action="store_true",
                         help="скрыть окно (2ГИС чаще показывает капчу headless-браузеру)")
    browser.add_argument("--channel", default="chrome", choices=["chrome", "msedge", "chromium"])
    browser.add_argument("--proxies", help="файл с прокси, по одному на строку")
    browser.add_argument("--proxy", help="один прокси, например http://user:pass@host:port")
    browser.add_argument("--max-pages", type=int, default=10_000,
                         help="предохранитель: максимум страниц на один запрос")

    catalog = argparse.ArgumentParser(add_help=False)
    catalog.add_argument("-q", "--query", action="append", dest="queries",
                         help="только эти запросы вместо рубрикатора и SEED_QUERIES")
    catalog.add_argument("--no-expand", action="store_true",
                         help="не дособирать рубрики и здания, найденные в карточках")
    catalog.add_argument("--no-buildings", action="store_true",
                         help="не обходить вкладку «В здании» у зданий")
    catalog.add_argument("--max-tasks", type=int, help="остановиться после N задач (для проверки)")
    catalog.add_argument("--fresh", action="store_true", help="очистить очередь города и начать заново")

    transport = argparse.ArgumentParser(add_help=False)
    transport.add_argument("--subtypes", default=",".join(TRANSPORT_SUBTYPES),
                           help="bus,metro,trolleybus,tram,shuttle_bus или all")

    parser = argparse.ArgumentParser(description="Браузерный парсер 2ГИС -> PostgreSQL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", parents=[common], help="создать схему и таблицы")
    stats = sub.add_parser("stats", parents=[common], help="статистика и полнота обхода")
    stats.add_argument("-r", "--region", help="только этот город")
    sub.add_parser("catalog", parents=[common, browser, catalog], help="все объекты города")
    sub.add_parser("transport", parents=[common, browser, transport], help="остановки и маршруты")
    sub.add_parser("crawl", parents=[common, browser, catalog, transport], help="transport + catalog")
    shard = argparse.ArgumentParser(add_help=False)
    shard.add_argument("--shard", type=parse_shard, default=(0, 1),
                       help="i/N: этот процесс берёт только свою долю объектов (для N параллельных процессов с разными прокси)")
    reviews = sub.add_parser("reviews", parents=[common, browser, shard], help="отзывы и комментарии сохранённых объектов")
    reviews.add_argument("--limit", type=int, help="не больше N объектов")
    cards = sub.add_parser("cards", parents=[common, browser, shard],
                           help="полные карточки (телефоны, соцсети, всё) для объектов без них")
    cards.add_argument("--limit", type=int, help="не больше N объектов")
    area = sub.add_parser("area", parents=[common, browser],
                          help="все объекты района: скан карты, «В здании», карточки, отзывы")
    area.add_argument("--area", required=True, help="название района/микрорайона или его ID из URL 2ГИС")
    area.add_argument("--no-reviews", action="store_true", help="без отзывов")
    area.add_argument("--rescan", action="store_true", help="сканировать карту заново, а не брать сохранённый скан")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_arguments()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    storage = SyncStorage(dsn=args.dsn, schema=args.schema)
    if not storage.is_configured:
        raise SystemExit("Не задан PG_DSN (в .env или --dsn).")
    storage.init_db()

    if args.command == "init-db":
        print_stats(storage, None)
        return
    if args.command == "stats":
        print_stats(storage, resolve_cities(args.region, None)[0] if args.region else None)
        return

    cities = resolve_cities(args.region, args.cities)
    subtypes = tuple(t.strip() for t in getattr(args, "subtypes", "").split(",") if t.strip())
    with DgisBrowserScraper(args.headless, args.channel, load_proxies(args.proxies, args.proxy),
                            map_mode=args.command == "area") as scraper:
        crawler = DgisCrawler(scraper, storage)
        for city in cities:
            logger.info("=== %s (%s): %s ===", city.name, city.slug, args.command)
            if args.command in ("transport", "crawl"):
                crawler.crawl_transport(city, subtypes, args.max_pages)
            if args.command in ("catalog", "crawl"):
                crawler.crawl_catalog(
                    city,
                    queries=args.queries,
                    expand=not args.no_expand,
                    buildings=not args.no_buildings,
                    max_pages=args.max_pages,
                    max_tasks=args.max_tasks,
                    fresh=args.fresh,
                )
            if args.command == "reviews":
                branches = storage.branches_for_reviews(city.name, args.limit, args.shard)
                logger.info("Объектов с несобранными отзывами: %d", len(branches))
                crawler.crawl_reviews(city, branches)
            if args.command == "cards":
                crawler.enrich_cards(city, storage.city_branches_without_card(city.name, args.limit, args.shard))
            if args.command == "area":
                crawler.crawl_area(city, args.area, with_reviews=not args.no_reviews, rescan=args.rescan)
            print_stats(storage, city)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Прервано (Ctrl+C). Прогресс в web_crawl_tasks, повторный запуск продолжит обход.")
