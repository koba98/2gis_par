"""
CLI браузерного парсера 2ГИС.

Подкоманды:
    init-db     создать схему, таблицы, индексы и подписи в PostgreSQL
    kz          весь Казахстан по очереди: волна 1 — крупные города, 2 — областные центры,
                3 — малые города, 4 — добор неполного; каждый город — транспорт, каталог,
                «В здании», карточки и отзывы. Можно запускать несколько воркеров
    plan        состояние плана kz: какие города и этапы выполнены
    area        все объекты одного района: скан карты, «В здании», полные карточки, отзывы
    catalog     все объекты города: рубрики, площадки, ЖК, организации внутри зданий
    cards       полные карточки (телефоны, соцсети, email, атрибуты) для объектов без них
    transport   остановки и маршруты (автобус, метро, LRT, троллейбус, трамвай, маршрутка, электричка)
    crawl       transport + catalog
    reviews     отзывы и комментарии к ним для объектов, уже сохранённых в БД
    stats       объём данных, категории и полнота: заявлено 2ГИС / собрано по каждому городу

Подключение к БД — в .env (PG_DSN, PG_SCHEMA).
"""

import argparse
import logging
import os
import random
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from parser import (CITIES, TRANSPORT_SUBTYPES, CaptchaBlockedError, City, DgisBrowserScraper, DgisCrawler,
                    NetworkDownError)
from storage import SyncStorage

logger = logging.getLogger("dgis_main")

KZ_STAGES = ("transport", "catalog", "buildings", "details")
RECHECK_TIER = 4
CAPTCHA_BACKOFF_MIN = (15, 30, 60, 120)  # пауза этапа после капчи подряд: 15 мин, 30, 60, потом каждые 2 ч


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
        return list(CITIES)
    names = [region] if region else [c.strip() for c in (cities or "").split(",") if c.strip()]
    if not names:
        raise SystemExit("Укажите город: -r 'Астана' или --cities 'Астана,Алматы' или --cities all")

    result: Dict[str, City] = {}
    for name in names:
        match = next((c for c in CITIES if name.lower() in (c.region.lower(), c.name.lower(), c.slug)), None)
        if match is None:
            raise SystemExit(f"Неизвестный город/регион: {name}. Доступны: {', '.join(c.name for c in CITIES)}")
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
        logger.info("Очередь обхода (заявлено 2ГИС / собрано):")
        for row in tasks:
            logger.info("  %-16s %-10s %-11s задач %-6d %d / %d",
                        row["city_slug"], row["kind"], row["status"], row["tasks"],
                        row["total"], row["collected"])
    print_completeness(storage, city)
    logger.info("=" * 70)


def print_completeness(storage: SyncStorage, city: Optional[City]) -> None:
    rows = [r for r in storage.completeness() if city is None or r["city_slug"] == city.slug]
    if not rows:
        return
    logger.info("Полнота по городам (объекты 2ГИС: заявлено / собрано; карточки и отзывы — доля собранных):")
    logger.info("  %-18s %17s %6s %9s %9s %11s %9s", "город", "объекты", "%", "карточки", "отзывы",
                "маршруты", "задач")
    for r in rows:
        logger.info("  %-18s %8s / %-6d %5s%% %8s%% %8s%% %5s / %-4d %4d/%d",
                    r["city"], r["declared_branches"], r["branches"], r["branches_pct"] or 0,
                    r["cards_pct"] or 0, r["reviews_pct"] or 0, r["declared_routes"], r["routes"],
                    r["tasks_open"], r["tasks_incomplete"])


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
    browser.add_argument("--proxy", default=os.getenv("DGIS_PROXY"), help="один прокси (env DGIS_PROXY)")
    browser.add_argument("--max-pages", type=int, default=10_000,
                         help="предохранитель: максимум страниц на один запрос")
    browser.add_argument("--pace", type=float, default=float(os.getenv("DGIS_PACE", "1.0")),
                         help="множитель антибан-пауз: 1.0 — как есть, 1.5 — осторожнее (env DGIS_PACE)")
    browser.add_argument("--captcha-wait", type=int, default=int(os.getenv("DGIS_CAPTCHA_WAIT", "120")),
                         help="сколько секунд ждать, пока капчу решат в окне; 0 — на сервере без человека")

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
                           help=f"{','.join(TRANSPORT_SUBTYPES)} или all")

    parser = argparse.ArgumentParser(description="Браузерный парсер 2ГИС -> PostgreSQL")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", parents=[common], help="создать схему и таблицы")
    stats = sub.add_parser("stats", parents=[common], help="статистика и полнота обхода")
    stats.add_argument("-r", "--region", help="только этот город")
    sub.add_parser("plan", parents=[common], help="состояние плана kz")
    kz = sub.add_parser("kz", parents=[common, browser],
                        help="весь Казахстан по волнам: крупные города, областные центры, малые города, добор")
    kz.add_argument("--tiers", default="1,2,3,4",
                    help="какие волны брать: 1 крупные, 2 областные центры, 3 малые города, 4 добор (по умолчанию все)")
    kz.add_argument("--worker", default=os.getenv("DGIS_WORKER") or socket.gethostname(),
                    help="имя воркера в плане (env DGIS_WORKER)")
    kz.add_argument("--once", action="store_true", help="выполнить один этап и выйти")
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


def kz_plan_jobs() -> List[Tuple[str, str, int, int]]:
    """(город, этап, волна, порядок): волны 1–3 по CITIES, затем волна 4 — добор по всем городам."""
    jobs = []
    for tier in sorted({c.tier for c in CITIES}):
        for city in (c for c in CITIES if c.tier == tier):
            for stage in KZ_STAGES:
                jobs.append((city.slug, stage, tier, len(jobs)))
    for city in CITIES:
        jobs.append((city.slug, "recheck", RECHECK_TIER, len(jobs)))
    return jobs


def run_kz(storage: SyncStorage, crawler: DgisCrawler, tiers: List[int], worker: str, once: bool) -> None:
    """
    Воркер плана: берёт следующий этап (город × этап) и выполняет его. Капча или обрыв сети —
    этап откладывается (15 мин, 30, 60, далее каждые 2 ч) и воркер берёт следующий доступный;
    прогресс внутри этапа сохранён в очередях. План общий в БД: воркеров может быть несколько.
    """
    by_slug = {c.slug: c for c in CITIES}
    storage.init_plan(kz_plan_jobs())
    blocked_in_row = 0
    while True:
        job = storage.claim_plan_job(tiers, worker)
        if job is None:
            left = [r for r in storage.plan_status() if r["tier"] in tiers and r["status"] != "done"]
            if not left:
                logger.info("План kz выполнен для волн %s.", ",".join(map(str, tiers)))
                return
            if once:
                return
            logger.info("Свободных этапов нет (ждут: %d, заняты другими воркерами или отложены) — ждём 5 мин",
                        len(left))
            time.sleep(300)
            continue

        city, stage = by_slug[job["city_slug"]], job["stage"]
        logger.info("=== [волна %d] %s (%s): этап %s, попытка %d ===",
                    job["tier"], city.name, city.slug, stage, job["attempts"])
        last_touch = [0.0]

        def heartbeat() -> None:
            if time.monotonic() - last_touch[0] > 60:
                storage.touch_plan_job(city.slug, stage)
                last_touch[0] = time.monotonic()

        crawler.heartbeat = heartbeat
        try:
            finished = crawler.run_stage(city, stage)
        except (CaptchaBlockedError, NetworkDownError) as e:
            delay = CAPTCHA_BACKOFF_MIN[min(blocked_in_row, len(CAPTCHA_BACKOFF_MIN) - 1)]
            blocked_in_row += 1
            storage.finish_plan_job(city.slug, stage, "pending", error=str(e)[:500], delay_minutes=delay)
            logger.error("%s — этап %s/%s отложен на %d мин", e, city.slug, stage, delay)
            # блокируют IP, а не этап: пауза и для самого воркера (с разбросом, чтобы воркеры не шли строем)
            time.sleep(delay * 60 * random.uniform(0.8, 1.0))
            if once:
                return
            continue
        except KeyboardInterrupt:
            storage.finish_plan_job(city.slug, stage, "pending", error="прервано вручную")
            raise
        except Exception as e:
            storage.finish_plan_job(city.slug, stage, "pending", error=f"{type(e).__name__}: {e}"[:500],
                                    delay_minutes=10)
            logger.exception("Этап %s/%s упал — повтор через 10 мин", city.slug, stage)
            if once:
                return
            continue
        finally:
            crawler.heartbeat = lambda: None

        blocked_in_row = 0
        storage.finish_plan_job(city.slug, stage, "done" if finished else "pending")
        logger.info("Этап %s/%s: %s", city.slug, stage, "выполнен" if finished else "остались задачи — продолжится")
        print_completeness(storage, city)
        if once:
            return


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
    if args.command == "plan":
        storage.init_plan(kz_plan_jobs())
        for r in storage.plan_status():
            logger.info("  волна %d  %-16s %-9s %-7s попыток %-3d %s %s", r["tier"], r["city_slug"], r["stage"],
                        r["status"], r["attempts"], r["finished"] or r["started"] or "", r["error"] or "")
        print_completeness(storage, None)
        return

    cities = [] if args.command == "kz" else resolve_cities(args.region, args.cities)
    subtypes = tuple(t.strip() for t in getattr(args, "subtypes", "").split(",") if t.strip())
    with DgisBrowserScraper(args.headless, args.channel, load_proxies(args.proxies, args.proxy),
                            map_mode=args.command == "area", pace_factor=args.pace,
                            captcha_wait=args.captcha_wait) as scraper:
        crawler = DgisCrawler(scraper, storage)
        if args.command == "kz":
            run_kz(storage, crawler, [int(t) for t in args.tiers.split(",") if t.strip()], args.worker, args.once)
            return
        for city in cities:
            logger.info("=== %s (%s): %s ===", city.name, city.slug, args.command)
            crawler.ensure_region(city)
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
            if args.command in ("reviews", "cards"):
                rows = storage.objects_for_details(int(city.region_id), city.name, args.limit,
                                                   need=args.command, shard=args.shard)
                logger.info("Объектов %s: %d", "с несобранными отзывами" if args.command == "reviews"
                            else "без полной карточки", len(rows))
                crawler.crawl_details(city, rows, reviews=args.command == "reviews")
            if args.command == "area":
                crawler.crawl_area(city, args.area, with_reviews=not args.no_reviews, rescan=args.rescan)
            print_stats(storage, city)


if __name__ == "__main__":
    try:
        main()
    except (CaptchaBlockedError, NetworkDownError) as e:
        logger.error("%s Прогресс сохранён — запустите ту же команду позже (или с другого IP / через --proxy).", e)
        sys.exit(2)
    except KeyboardInterrupt:
        logger.info("Прервано (Ctrl+C). Прогресс в web_crawl_tasks, повторный запуск продолжит обход.")
