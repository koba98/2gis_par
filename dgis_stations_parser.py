#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Парсер остановок и автобусных маршрутов 2GIS по регионам Казахстана
=====================================================================

Как это устроено (проверено на реальных данных пользователя):

1. Страница https://2gis.kz/<город>/search/остановка[/page/N] содержит
   в HTML (не в отдельном XHR) блок:

       var initialState = JSON.parse('{...}');

   Внутри — данные по каждой остановке на странице: имя, точные
   координаты (point.lat/lon) и СПИСОК АВТОБУСОВ, которые на ней
   останавливаются (routes: [{id, name, subtype, from_name, to_name}]).
   Парсинг этого блока (включая разбор двойного экранирования кавычек
   у названий вида ЖК "Орман парк") проверен на реальном сохранённом
   HTML с 2gis.kz — работает.

   Логика та же, что предложил сам пользователь: собираем все остановки
   города, у каждой список автобусов -> группируем по номеру автобуса ->
   это и есть маршрут (набор его остановок).

2. Прямые HTTP-запросы (requests) 2GIS показывает заглушку "обновите
   браузер" (её отдаёт сервер по заголовкам запроса, а кнопку
   "Пропустить" обрабатывает отдельный JS-файл museum.js — то есть
   без реального выполнения JS этот экран не обойти). Поэтому здесь
   используется Playwright с УЖЕ УСТАНОВЛЕННЫМ системным Chrome
   (channel="chrome") — это, во-первых, реальный браузер (проходит
   заглушку кликом по кнопке), во-вторых, не требует playwright install
   (которая на корпоративной сети падает из-за антивируса/прокси,
   подменяющего SSL-сертификаты).

Использование:
    pip install playwright psycopg2-binary --break-system-packages
    # playwright install chromium — НЕ нужен, используем системный Chrome

    # впиши свои креды PostgreSQL в DB_CONFIG внизу секции констант (или
    # оставь пустым — тогда просто сохранится CSV без заливки в БД)

    # тест на 1 городе, маленький лимит страниц, с окном браузера
    python dgis_stations_parser.py --cities Астана --max-pages 3 --out-dir ./test_run

    # боевой прогон по всем регионам (без лимита страниц, льётся в БД если
    # заполнен DB_CONFIG, таблица web_parsing."2gis_bus_stations")
    python dgis_stations_parser.py --cities all --out-dir ./output
"""

import argparse
import base64
import csv
import json
import logging
import random
import re
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dgis_stations")

# ---------------------------------------------------------------------------
# Антибан: кулдаун между страницами + разные viewport на контекст
# ---------------------------------------------------------------------------

COOLDOWN_MIN = 1.5
COOLDOWN_MAX = 2.6

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]


def cooldown():
    time.sleep(random.uniform(COOLDOWN_MIN, COOLDOWN_MAX))


# ---------------------------------------------------------------------------
# Регионы Казахстана (те же 20 — проверь слаги вживую перед боевым прогоном)
# ---------------------------------------------------------------------------

REGIONS = {
    "Астана": ("Астана", "astana"),
    "Алматы": ("Алматы", "almaty"),
    "Шымкент": ("Шымкент", "shymkent"),
    "Акмолинская область": ("Кокшетау", "kokshetau"),
    "Актюбинская область": ("Актобе", "aktobe"),
    "Алматинская область": ("Талдыкорган", "taldykorgan"),
    "Атырауская область": ("Атырау", "atyrau"),
    "Восточно-Казахстанская область": ("Усть-Каменогорск", "ust-kamenogorsk"),
    "Жамбылская область": ("Тараз", "taraz"),
    "Жетысуская область": ("Талдыкорган", "taldykorgan"),
    "Западно-Казахстанская область": ("Уральск", "uralsk"),
    "Карагандинская область": ("Караганда", "karaganda"),
    "Костанайская область": ("Костанай", "kostanay"),
    "Кызылординская область": ("Кызылорда", "kyzylorda"),
    "Мангистауская область": ("Актау", "aktau"),
    "Павлодарская область": ("Павлодар", "pavlodar"),
    "Северо-Казахстанская область": ("Петропавловск", "petropavlovsk"),
    "Туркестанская область": ("Туркестан", "turkestan"),
    "Улытауская область": ("Жезказган", "zhezkazgan"),
    "Абайская область": ("Семей", "semey"),
}

SEARCH_QUERY = "остановка автобуса"


# ---------------------------------------------------------------------------
# PostgreSQL — впиши свои креды сюда (или оставь пустым и заливка сама
# пропустится с предупреждением, CSV в любом случае сохранится)
# ---------------------------------------------------------------------------

DB_CONFIG = {
    "host": os.getenv("PG_HOST", ""),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DB", ""),
    "user": os.getenv("PG_USER", ""),
    "password": os.getenv("PG_PASSWORD", ""),
}


DB_SCHEMA = "web_parsing"
DB_TABLE = "2gis_bus_stations"  # имя начинается с цифры -> в SQL везде в двойных кавычках


def _db_configured() -> bool:
    return bool(DB_CONFIG["host"] and DB_CONFIG["dbname"] and DB_CONFIG["user"])


def _dsn() -> str:
    return (
        f"host={DB_CONFIG['host']} port={DB_CONFIG['port']} "
        f"dbname={DB_CONFIG['dbname']} user={DB_CONFIG['user']} "
        f"password={DB_CONFIG['password']}"
    )


CREATE_TABLE_SQL = f'''
CREATE TABLE IF NOT EXISTS {DB_SCHEMA}."{DB_TABLE}" (
    id SERIAL PRIMARY KEY,
    region TEXT,
    city TEXT,
    city_slug TEXT,
    route_id TEXT,
    route_number TEXT,
    route_subtype TEXT,
    route_from TEXT,
    route_to TEXT,
    stop_id TEXT,
    stop_name TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    district TEXT,
    scraped_at TIMESTAMP DEFAULT now(),
    UNIQUE (city_slug, route_id, stop_id)
);
'''

UPSERT_SQL = f'''
INSERT INTO {DB_SCHEMA}."{DB_TABLE}"
    (region, city, city_slug, route_id, route_number, route_subtype,
     route_from, route_to, stop_id, stop_name, lat, lon, district)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (city_slug, route_id, stop_id) DO UPDATE SET
    region = EXCLUDED.region,
    city = EXCLUDED.city,
    route_number = EXCLUDED.route_number,
    route_subtype = EXCLUDED.route_subtype,
    route_from = EXCLUDED.route_from,
    route_to = EXCLUDED.route_to,
    stop_name = EXCLUDED.stop_name,
    lat = EXCLUDED.lat,
    lon = EXCLUDED.lon,
    district = EXCLUDED.district,
    scraped_at = now();
'''


def save_postgres(rows: list):
    if not _db_configured():
        log.warning(
            "DB_CONFIG не заполнен (host/dbname/user пустые) — пропускаю заливку в PostgreSQL. "
            "Данные всё равно сохранены в CSV. Впиши креды в DB_CONFIG в начале файла, если нужна БД."
        )
        return

    import psycopg2

    conn = psycopg2.connect(_dsn())
    cur = conn.cursor()
    try:
        cur.execute(CREATE_TABLE_SQL)
        for r in rows:
            cur.execute(UPSERT_SQL, (
                r["region"], r["city"], r["city_slug"], r["route_id"], r["route_number"],
                r["route_subtype"], r["route_from"], r["route_to"], r["stop_id"],
                r["stop_name"], r["lat"], r["lon"], r["district"],
            ))
        conn.commit()
        log.info("Загружено в PostgreSQL (%s.\"%s\"): %s строк", DB_SCHEMA, DB_TABLE, len(rows))
    except Exception as e:
        conn.rollback()
        log.error("Ошибка записи в PostgreSQL: %s", e)
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Извлечение embedded initialState из HTML (проверено на реальном файле)
# ---------------------------------------------------------------------------

def extract_initial_state(html_text: str) -> Optional[dict]:
    marker = "var initialState = JSON.parse('"
    start = html_text.find(marker)
    if start == -1:
        return None

    i = start + len(marker)
    n = len(html_text)
    buf = []
    while i < n:
        ch = html_text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(html_text[i + 1])
            i += 2
            continue
        if ch == "'":
            break
        buf.append(ch)
        i += 1
    raw = "".join(buf)

    # Лишний уровень экранирования кавычек внутри значений (\\" -> \")
    # и экранированный апостроф JS (\' -> '), который не нужен в JSON.
    json_text = raw.replace('\\\\"', '\\"')
    json_text = json_text.replace("\\'", "'")

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        log.error("Не смог распарсить initialState: %s (позиция %s)", e, e.pos)
        return None


def extract_stops_from_state(state: dict) -> list:
    stops = []
    try:
        profile = state["data"]["entity"]["profile"]
    except (KeyError, TypeError):
        return stops

    for pid, entry in profile.items():
        d = (entry or {}).get("data") or {}
        point = d.get("point")
        routes = d.get("routes")
        name = d.get("name")
        if not point or not routes:
            continue  # не остановка транспорта (станция LRT, реклама и т.п.)

        district = None
        for adm in d.get("adm_div", []) or []:
            if adm.get("type") == "district":
                district = adm.get("name")

        stops.append({
            "stop_id": pid,
            "stop_name": name,
            "lat": point.get("lat"),
            "lon": point.get("lon"),
            "district": district,
            "routes": routes,
        })
    return stops


def update_route_meta(route_meta: dict, stops: list):
    """Копим справочник route_id -> {name, subtype, from_name, to_name},
    встреченный в любом валидном initialState — пригодится, чтобы
    достроить откуда-куда для маршрутов, найденных позже только через DOM."""
    for s in stops:
        for r in s.get("routes", []):
            rid = r.get("id")
            if rid and rid not in route_meta:
                route_meta[rid] = {
                    "name": r.get("name"),
                    "subtype": r.get("subtype"),
                    "from_name": r.get("from_name"),
                    "to_name": r.get("to_name"),
                }


def decode_stat_geo(href: str):
    """Достаёт lon/lat из base64 query-параметра stat= в ссылке на остановку
    (та же структура, что в initialState — placeItem.geoPosition)."""
    try:
        parsed = urllib.parse.urlparse(href)
        qs = urllib.parse.parse_qs(parsed.query)
        stat_b64 = qs.get("stat", [None])[0]
        if not stat_b64:
            return None, None
        padded = stat_b64 + "=" * (-len(stat_b64) % 4)
        raw = base64.b64decode(padded)
        data = json.loads(raw.decode("utf-8"))
        geo = (data.get("placeItem") or {}).get("geoPosition") or {}
        return geo.get("lat"), geo.get("lon")
    except Exception:
        return None, None


def extract_stops_from_dom(page: Page, route_meta: dict) -> list:
    """Запасной путь: читаем остановки прямо из отрисованного DOM, а не из
    var initialState. Похоже, на страницах глубже ~5 сервер не обновляет
    initialState (там остаётся что-то вроде дефолтного слепка), а реальные
    результаты подгружаются JS уже после первой отрисовки и есть только
    в живом DOM. Класс-имена (_zjunba, _18su5fr, _1i35oqm1) — это хэши
    CSS-модулей 2GIS, взятые из реального сохранённого HTML; если сайт
    обновит вёрстку, их может понадобиться поправить."""
    stops = []
    cards = page.locator("div._zjunba")
    try:
        count = cards.count()
    except Exception:
        return stops

    for i in range(count):
        card = cards.nth(i)
        try:
            anchor = card.locator("a").first
            href = anchor.get_attribute("href") or ""
            name = anchor.inner_text().strip()
        except Exception:
            continue

        m = re.search(r"/station/(\d+)", href)
        if not m:
            continue  # не остановка (обычная карточка фирмы/POI)
        stop_id = m.group(1)

        lat, lon = decode_stat_geo(href)
        if lat is None or lon is None:
            continue

        try:
            parent = card.locator("xpath=../..")
            route_links = parent.locator("ul._18su5fr a._1i35oqm1")
            r_count = route_links.count()
        except Exception:
            r_count = 0

        routes = []
        for j in range(r_count):
            r = route_links.nth(j)
            try:
                r_href = r.get_attribute("href") or ""
                r_number = r.inner_text().strip()
            except Exception:
                continue
            r_id_m = re.search(r"/route/(\d+)", r_href)
            if not r_id_m:
                continue
            rid = r_id_m.group(1)
            meta = route_meta.get(rid, {})
            routes.append({
                "id": rid,
                "name": meta.get("name") or r_number,
                "subtype": meta.get("subtype") or "bus",
                "from_name": meta.get("from_name"),
                "to_name": meta.get("to_name"),
            })

        if routes:
            stops.append({
                "stop_id": stop_id,
                "stop_name": name,
                "lat": lat,
                "lon": lon,
                "district": None,
                "routes": routes,
            })
    return stops


# ---------------------------------------------------------------------------
# Playwright: навигация + обход заглушки "обновите браузер"
# ---------------------------------------------------------------------------

def is_browser_wall(page: Page) -> bool:
    try:
        return "acceptRiskButton" in page.content()
    except Exception:
        return False


def bypass_browser_wall(page: Page):
    """Тот же экран, что и раньше в bus-парсере: жмём 'Пропустить обновление
    браузера и перейти в 2ГИС'. Кнопка обрабатывается их собственным JS
    (museum.js), поэтому обойти её можно только реальным кликом в браузере,
    не голым HTTP-запросом."""
    try:
        page.click("text=Пропустить обновление браузера и перейти в 2ГИС", timeout=5000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as e:
        log.warning("Не удалось пройти экран обновления браузера: %s", e)


class DgisStationsScraper:
    def __init__(self, headless: bool = True, channel: Optional[str] = "chrome",
                 out_dir: str = "./output", save_html: bool = False):
        self.headless = headless
        self.channel = channel
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.save_html = save_html
        self.html_dir = self.out_dir / "raw_html"
        if self.save_html:
            self.html_dir.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._browser = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        launch_kwargs = {"headless": self.headless}
        if self.channel and self.channel != "chromium":
            launch_kwargs["channel"] = self.channel
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def open_city_page(self) -> Page:
        """Один контекст (куки/localStorage) на весь город — это важно:
        2GIS, похоже, привязывает продолжение пагинации к состоянию сессии,
        и если открывать каждую страницу в новом 'чистом' контексте (как
        было раньше), сайт после 5-6 страниц перестаёт отдавать новые данные,
        хотя реальных страниц там больше (проверено — в браузере пагинация
        реально доходит минимум до 10+)."""
        context = self._browser.new_context(
            viewport=random.choice(VIEWPORTS),
            locale="ru-RU",
        )
        return context.new_page()

    def load_page(self, page: Page, url: str, tag: str = "page", retries: int = 3) -> Optional[str]:
        for attempt in range(1, retries + 1):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if is_browser_wall(page):
                    log.warning("Поймали заглушку 'обновите браузер' — обходим кликом")
                    bypass_browser_wall(page)

                # Даём JS время дорисовать карточки (особенно важно для глубоких
                # страниц, где initialState в HTML не отражает реальные данные).
                try:
                    page.wait_for_selector("div._zjunba", timeout=10000)
                except Exception:
                    pass
                page.wait_for_timeout(800)

                html_text = page.content()

                if self.save_html:
                    (self.html_dir / f"{tag}_attempt{attempt}.html").write_text(
                        html_text, encoding="utf-8", errors="replace"
                    )

                if "var initialState" in html_text:
                    return html_text

                log.warning(
                    "initialState не найден в ответе (попытка %s/%s) — возможно, "
                    "страница не успела прогрузиться или разметка другая", attempt, retries,
                )
            except Exception as e:
                log.warning("Ошибка загрузки %s: %s (попытка %s/%s)", url, e, attempt, retries)
            cooldown()
        return None


# ---------------------------------------------------------------------------
# Обход страниц одного города
# ---------------------------------------------------------------------------

def crawl_city_stations(scraper: DgisStationsScraper, city_slug: str, max_pages: int = 200) -> list:
    stops_by_id = {}
    route_meta = {}
    empty_streak = 0
    page_num = 1

    page = scraper.open_city_page()
    try:
        # Страница 1 — обычная полная навигация. Это единственный раз, когда
        # var initialState в HTML реально соответствует показанным данным.
        url = f"https://2gis.kz/{city_slug}/search/{SEARCH_QUERY}"
        log.info("Город %s, страница %s: %s", city_slug, page_num, url)
        html_text = scraper.load_page(page, url, tag=f"{city_slug}_page{page_num}")
        cooldown()

        if html_text is None:
            log.error("Не удалось получить страницу %s — пропускаем город %s", url, city_slug)
            return []

        while True:
            before = len(stops_by_id)

            if page_num == 1:
                state = extract_initial_state(html_text)
                state_stops = extract_stops_from_state(state) if state else []
                update_route_meta(route_meta, state_stops)
                for s in state_stops:
                    stops_by_id.setdefault(s["stop_id"], s)
            else:
                state_stops = []

            # Для страниц, куда попали кликом (без полной перезагрузки),
            # initialState не обновляется — источник истины тут только DOM.
            dom_stops = extract_stops_from_dom(page, route_meta)
            new_from_dom = 0
            for s in dom_stops:
                if s["stop_id"] not in stops_by_id:
                    stops_by_id[s["stop_id"]] = s
                    new_from_dom += 1

            after = len(stops_by_id)
            log.info(
                "  страница %s — initialState=%s, DOM=%s (новых через DOM: %s), "
                "новых всего: %s, всего по городу: %s",
                page_num, len(state_stops), len(dom_stops), new_from_dom, after - before, after,
            )

            if after == before:
                empty_streak += 1
                if empty_streak >= 3:
                    log.info("3 страницы подряд без новых остановок — заканчиваем город %s", city_slug)
                    break
            else:
                empty_streak = 0

            if page_num >= max_pages:
                break

            # Переходим на следующую страницу КЛИКОМ по её ссылке в пагинации
            # (а не через page.goto на новый URL) — так же, как это делает
            # живой пользователь. Похоже, именно полные перезагрузки страницы
            # (goto) попадают под более строгий лимит на автоматизированные
            # запросы, а клики внутри SPA — нет.
            next_num = page_num + 1
            try:
                page.click(f"a[href$='/page/{next_num}']", timeout=5000)
            except Exception as e:
                log.info(
                    "Не нашли ссылку на страницу %s (похоже, страницы закончились): %s",
                    next_num, e,
                )
                break

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            try:
                page.wait_for_selector("div._zjunba", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            cooldown()

            page_num = next_num
    finally:
        page.close()

    return list(stops_by_id.values())


# ---------------------------------------------------------------------------
# Сборка финальной плоской таблицы: маршрут x остановка
# ---------------------------------------------------------------------------

def build_route_stop_rows(region: str, city_name: str, city_slug: str, stops: list,
                           subtypes: tuple = ("bus",)) -> list:
    rows = []
    for s in stops:
        for r in s["routes"]:
            if subtypes and r.get("subtype") not in subtypes:
                continue
            rows.append({
                "region": region,
                "city": city_name,
                "city_slug": city_slug,
                "route_id": r.get("id"),
                "route_number": r.get("name"),
                "route_subtype": r.get("subtype"),
                "route_from": r.get("from_name"),
                "route_to": r.get("to_name"),
                "stop_id": s["stop_id"],
                "stop_name": s["stop_name"],
                "lat": s["lat"],
                "lon": s["lon"],
                "district": s["district"],
            })
    return rows


def save_csv(rows: list, out_path: Path):
    fieldnames = ["region", "city", "city_slug", "route_id", "route_number", "route_subtype",
                  "route_from", "route_to", "stop_id", "stop_name", "lat", "lon", "district"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    log.info("Сохранено: %s (%s строк)", out_path, len(rows))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Парсер остановок и автобусных маршрутов 2GIS")
    parser.add_argument("--cities", default="all",
                         help="'all' или список регионов через запятую, например: 'Астана,Алматы'")
    parser.add_argument("--max-pages", type=int, default=100000,
                         help="Практически без лимита — реальную остановку делает логика "
                              "'2 страницы подряд без новых остановок'. Этот параметр — просто "
                              "аварийный предохранитель от бесконечного цикла.")
    parser.add_argument("--out-dir", default="./output")
    parser.add_argument("--save-html", action="store_true",
                         help="Сохранять HTML каждой попытки (для диагностики)")
    parser.add_argument("--headless", action="store_true",
                         help="Скрыть окно браузера. ВНИМАНИЕ: 2GIS, похоже, детектит headless-режим "
                              "даже в настоящем Chrome и показывает заглушку каждый раз — по умолчанию "
                              "окно видимое, так как это надёжно работает.")
    parser.add_argument("--channel", default="chrome", choices=["chrome", "msedge", "chromium"],
                         help="chrome/msedge — системный браузер (по умолчанию, без playwright install); "
                              "chromium — бандл playwright (нужен playwright install chromium)")
    parser.add_argument("--subtypes", default="bus",
                         help="Через запятую: bus,trolleybus,tram,shuttle_bus — какие типы транспорта "
                              "включать в итоговую таблицу (по умолчанию только автобусы)")
    args = parser.parse_args()

    if args.cities.strip().lower() == "all":
        regions = REGIONS
    else:
        wanted = [c.strip() for c in args.cities.split(",")]
        regions = {k: v for k, v in REGIONS.items() if k in wanted}
        missing = set(wanted) - set(regions.keys())
        if missing:
            log.warning("Регионы не найдены в справочнике REGIONS: %s", missing)

    subtypes = tuple(t.strip() for t in args.subtypes.split(",") if t.strip())

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    with DgisStationsScraper(headless=args.headless, channel=args.channel,
                              out_dir=str(out_dir), save_html=args.save_html) as scraper:
        for region, (city_name, city_slug) in regions.items():
            log.info("=== Регион: %s (город: %s) ===", region, city_slug)
            try:
                stops = crawl_city_stations(scraper, city_slug, max_pages=args.max_pages)
            except Exception as e:
                log.error("Ошибка на регионе %s (%s): %s", region, city_slug, e)
                continue
            log.info("Итого остановок с автобусами по городу %s: %s", city_slug, len(stops))
            all_rows.extend(build_route_stop_rows(region, city_name, city_slug, stops, subtypes))

    out_path = out_dir / "dgis_routes_stops.csv"
    save_csv(all_rows, out_path)
    save_postgres(all_rows)


if __name__ == "__main__":
    main()