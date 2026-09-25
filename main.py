"""
Главная точка входа для автономного парсера 2ГИС.
Предоставляет гибкий CLI-интерфейс, управление параметрами поиска,
настройку прокси, троттлинга и безопасное завершение работы (graceful shutdown).
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import List, Optional

from client import AntiBanHttpClient
from database import Database
from parser import TwoGisParser, DEFAULT_CATALOG_KEY, DEFAULT_REVIEWS_KEY


def setup_logging(verbose: bool = False) -> None:
    """Настраивает форматированный вывод логов в консоль."""
    level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
    logging.basicConfig(
        level=level,
        format=log_format,
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


async def run_parser(args: argparse.Namespace) -> None:
    """Асинхронная корутина запуска и координации компонентов парсера."""
    proxies = load_proxies_from_file(args.proxies)

    db = Database(db_path=args.db)
    client = AntiBanHttpClient(
        proxies=proxies,
        base_delay=args.delay,
        max_delay=args.max_delay,
        request_timeout=args.timeout,
    )

    parser = TwoGisParser(
        client=client,
        db=db,
        catalog_key=args.catalog_key or DEFAULT_CATALOG_KEY,
        reviews_key=args.reviews_key or DEFAULT_REVIEWS_KEY,
    )

    # Регистрация обработчиков завершения для безопасного выхода
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        logging.warning("Получен сигнал прерывания. Завершаем текущие задачи...")
        stop_event.set()

    # Для Windows SIGINT перехватывается стандартным try/except, на Unix можно через add_signal_handler
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_signal)

    try:
        async with db:
            async with client:
                logging.info("=" * 60)
                logging.info("2GIS Autonomous Catalog & Reviews Parser")
                logging.info("=" * 60)
                logging.info("Поисковый запрос: '%s'", args.query)
                logging.info("Локация (lon,lat): %s", args.location or "Не указана")
                logging.info("Целевой лимит организаций: %d", args.limit)
                logging.info("Сбор отзывов: %s", "Отключен" if args.no_reviews else f"Включен (до {args.max_reviews} на объект)")
                logging.info("Файл БД: %s", args.db)
                logging.info("Активных прокси: %d", client.proxy_manager.total_count)
                logging.info("=" * 60)

                parse_task = asyncio.create_task(
                    parser.search_and_parse(
                        query=args.query,
                        location=args.location,
                        city_id=args.city_id,
                        max_places=args.limit,
                        page_size=args.page_size,
                        fetch_comments=not args.no_reviews,
                        max_reviews_per_place=args.max_reviews,
                    )
                )

                # Ожидаем либо завершения парсинга, либо сигнала остановки
                done, pending = await asyncio.wait(
                    [parse_task, asyncio.create_task(stop_event.wait())],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if stop_event.is_set():
                    parse_task.cancel()
                    logging.info("Операция парсинга была прервана пользователем.")

                stats = await db.get_stats()
                logging.info("=" * 60)
                logging.info("ИТОГИ СБОРА:")
                logging.info("Организаций в БД: %d", stats["places"])
                logging.info("Отзывов в БД:      %d", stats["comments"])
                logging.info("База данных сохранена: %s", os.path.abspath(args.db))
                logging.info("=" * 60)

    except asyncio.CancelledError:
        logging.info("Задачи были отменены.")
    except Exception as exc:
        logging.exception("Непредвиденная ошибка в процессе работы парсера: %s", exc)


def parse_arguments() -> argparse.Namespace:
    """Определяет аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Автономный асинхронный парсер каталога и отзывов 2ГИС с обходом блокировок."
    )
    parser.add_argument(
        "-q", "--query",
        type=str,
        default="кафе",
        help="Поисковый запрос (например: 'ресторан', 'аптека', 'автосервис', 'отель'). По умолчанию: 'кафе'",
    )
    parser.add_argument(
        "-l", "--location",
        type=str,
        default="37.6176,55.7558",
        help="Координаты центра поиска в формате 'lon,lat' (долгота, широта). По умолчанию: Москва '37.6176,55.7558'",
    )
    parser.add_argument(
        "--city-id",
        type=str,
        default=None,
        help="ID города в 2ГИС (например '4504222397630173' для Москвы).",
    )
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=10,
        help="Максимальное количество организаций для сбора. По умолчанию: 10",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=10,
        help="Размер страницы выдачи (1-50). По умолчанию: 10",
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        default=20,
        help="Максимальное число отзывов на одну организацию при пагинации. По умолчанию: 20",
    )
    parser.add_argument(
        "--no-reviews",
        action="store_true",
        help="Отключить сбор отзывов (собирать только организации).",
    )
    parser.add_argument(
        "--proxies",
        type=str,
        default=None,
        help="Путь к текстовому файлу со списком прокси (http://ip:port или http://user:pass@ip:port).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="2gis_data.db",
        help="Имя файла базы данных SQLite. По умолчанию: 2gis_data.db",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Базовая задержка троттлинга между запросами в секундах. По умолчанию: 1.0",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=30.0,
        help="Максимальная задержка бэкоффа при ошибках 429/403 в секундах. По умолчанию: 30.0",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Таймаут одного HTTP-запроса в секундах. По умолчанию: 15.0",
    )
    parser.add_argument(
        "--catalog-key",
        type=str,
        default=None,
        help="Пользовательский API-ключ для каталога (Places API).",
    )
    parser.add_argument(
        "--reviews-key",
        type=str,
        default=None,
        help="Пользовательский API-ключ для отзывов (Reviews API).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Включить подробный отладочный вывод (DEBUG).",
    )

    return parser.parse_args()


def main():
    """Точка входа CLI."""
    args = parse_arguments()
    setup_logging(args.verbose)

    try:
        asyncio.run(run_parser(args))
    except KeyboardInterrupt:
        logging.info("Работа программы прервана комбинацией клавиш Ctrl+C.")


if __name__ == "__main__":
    main()
