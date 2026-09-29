"""
Браузерный парсер 2ГИС (Playwright + системный Chrome, без API-ключей).

Как обеспечивается полнота (проверено на живом 2gis.kz):
- Поисковая выдача 2ГИС не обрезается: сайт сам сообщает total и pages
  (например, «магазин» в Астане — 4677 объектов на 390 страницах). Парсер
  проходит ВСЕ страницы кликами по пагинации и сверяет собранное с total.
- Прямой переход на /page/N сайт игнорирует (отдаёт первую страницу), поэтому
  страницы листаются только кликами, а данные берутся из XHR /3.0/items,
  которые сайт сам запрашивает при клике.
- Рубрики ищутся с фильтром rubricId — в выдаче только объекты этой рубрики.
  Новые рубрики берутся из карточек найденных объектов («снежный ком»).
- Для каждого здания/ЖК открывается вкладка «В здании» и догружается кнопкой
  «Загрузить ещё» до total — так собираются магазины, кафе и прочие точки
  внутри ЖК, а также объекты на территории (площадки, корты и т.д.).
- Очередь задач и их полнота хранятся в PostgreSQL (web_crawl_tasks),
  прерванный обход продолжается с места остановки.
"""

import csv
import math
import json
import logging
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from playwright.sync_api import Page, Response, sync_playwright

from storage import SyncStorage

logger = logging.getLogger("dgis_parser")

SITE = "https://2gis.kz"

# ---------------------------------------------------------------------------
# Антибан: паузы между действиями, длинные перерывы, разные viewport
# ---------------------------------------------------------------------------

COOLDOWN = (1.5, 2.6)          # пауза после загрузки страницы / клика по пагинации
SHORT_COOLDOWN = (0.6, 1.2)    # пауза между «Загрузить ещё»
LOAD_MORE = re.compile("Загрузить ещё|Показать ещё")
LONG_BREAK_EVERY = 150         # каждые N действий — длинный перерыв
LONG_BREAK = (25.0, 50.0)
CAPTCHA_WAIT_SEC = 120         # сколько ждать, пока человек решит капчу в окне
XHR_WAIT_SEC = 15              # сколько ждать ответ сайта после клика
SEARCH_PAGE_SIZE = 12          # объектов на странице поисковой выдачи 2ГИС
NETWORK_ERROR = re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION_\w+|ERR_NETWORK_\w+"
                           r"|ERR_ADDRESS_UNREACHABLE|ERR_TIMED_OUT|ERR_PROXY_CONNECTION_FAILED|ERR_TUNNEL_\w+")
NET_RETRY_SEC = 30             # пропала сеть: проверять каждые N секунд
NET_WAIT_MAX_SEC = 30 * 60     # и ждать не дольше 30 минут, потом остановиться с сохранением прогресса

# Скан района кликами по карте (калибровка: 18-й зум ≈ 0,37 м на пиксель в Астане)
SCAN_ZOOM = 18
SCAN_VIEWPORT = {"width": 1536, "height": 864}
SCAN_RECT = (440, 90, 1330, 780)   # часть экрана без панелей, рекламы и кнопок: x0, y0, x1, y1
SCAN_STEP_PX = 32                  # шаг сетки ≈ 12 м: мельче большинства зданий и площадок
SCAN_CLICK_PAUSE = (0.25, 0.5)

# Экономия ресурсов: не грузим то, что не несёт данных (картинки, шрифты, медиа,
# счётчики аналитики), тайлы карты — только в режиме скана района
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
_TRACKERS = (r"mc\.yandex\.|mail\.ru|google-analytics|analytics\.google|googletagmanager|doubleclick"
             r"|tns-counter|yadro\.ru|google\.[a-z.]+/(?:ads|pagead)|www\.google\.kz|facebook\.|vk\.com/rtrg")
TRACKER_HOSTS = re.compile(_TRACKERS)
TRACKER_AND_MAP_HOSTS = re.compile(_TRACKERS + r"|tile\d*\.maps\.2gis\.|jam\.api\.2gis\.|mapgl\.2gis\.com")
RECYCLE_EVERY = 40             # пересоздавать контекст каждые N загрузок: рендерер 2ГИС копит ~20 МБ на страницу
CHROME_ARGS = [
    "--disable-dev-shm-usage",           # в Docker /dev/shm маленький
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--mute-audio",
    "--no-first-run",
    "--disk-cache-size=67108864",        # 64 МБ дискового кэша на профиль
    "--renderer-process-limit=2",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

# ---------------------------------------------------------------------------
# Регионы Казахстана: название региона -> (город, slug на 2gis.kz)
# ---------------------------------------------------------------------------

REGIONS: Dict[str, Tuple[str, str]] = {
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

TRANSPORT_QUERIES: Dict[str, List[str]] = {
    "bus": ["остановка автобуса", "автобус"],
    "metro": ["метро", "станция метро"],
    "trolleybus": ["троллейбус", "остановка троллейбуса"],
    "tram": ["трамвай", "трамвайная остановка"],
    "shuttle_bus": ["маршрутка"],
}
TRANSPORT_SUBTYPES = tuple(TRANSPORT_QUERIES)

# Затравочные запросы для объектов без рубрик (площадки, территории, ЖК).
# Организации с рубриками дособираются через rubricId, здания — через «В здании».
SEED_QUERIES: List[str] = [
    "жилой комплекс", "жилой дом", "новостройки", "коттеджный городок",
    "парк", "сквер", "бульвар", "набережная", "ботанический сад",
    "баскетбольная площадка", "теннисный корт", "футбольное поле",
    "спортивная площадка", "детская площадка", "площадка для воркаута",
    "стадион", "скейтпарк", "каток", "беговая дорожка",
    "достопримечательность", "памятник", "монумент", "фонтан",
    "торговый центр", "бизнес-центр", "рынок",
    "школа", "детский сад", "университет", "колледж",
    "больница", "поликлиника", "ЦОН", "акимат",
    "парковка", "автозаправка",
]

# Типы объектов 2ГИС, которые сохраняются как карточки
OBJECT_TYPES = {"branch", "building", "attraction", "adm_div", "parking"}
# Административные единицы, которые НЕ являются объектами на карте
SKIP_ADM_SUBTYPES = {"country", "region", "city", "district", "district_area", "division", "settlement"}
TYPE_CATEGORY = {
    "building": "Здания",
    "attraction": "Объекты на территории",
    "adm_div": "Территории и площадки",
    "parking": "Парковки",
}

MAX_TASK_ATTEMPTS = 3


def _pause(bounds: Tuple[float, float]) -> None:
    time.sleep(random.uniform(*bounds))


def _frange(start: float, stop: float, step: float) -> Iterable[float]:
    value = start + step / 2
    while value < stop:
        yield value
        value += step


# ---------------------------------------------------------------------------
# Разбор данных 2ГИС
# ---------------------------------------------------------------------------

def _to_int(value: Any) -> Optional[int]:
    """ID 2ГИС в int; составные ID вида '70000001082550767_hash' обрезаются до числа."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip().split("_", 1)[0]
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_branch(item: Dict[str, Any], default_category: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Карточка объекта 2ГИС (организация, здание, площадка, территория) -> строка branches."""
    raw_id = str(item.get("id") or "")
    branch_id = _to_int(raw_id)
    if branch_id is None:
        return None

    name_ex = item.get("name_ex") or {}
    point = item.get("point") or {}
    address = item.get("address") or {}
    reviews = item.get("reviews") or {}
    adm_div = {d.get("type"): d.get("name") for d in item.get("adm_div") or [] if isinstance(d, dict)}

    org = None
    org_raw = item.get("org") or {}
    org_id = _to_int(org_raw.get("id"))
    if org_id is not None:
        org = {
            "id": org_id,
            "name": org_raw.get("name") or org_raw.get("primary"),
            "branch_count": org_raw.get("branch_count"),
            "raw": org_raw,
        }

    rubrics = []
    for r in item.get("rubrics") or []:
        rubric_id = _to_int(r.get("id")) if isinstance(r, dict) else None
        if rubric_id is None:
            continue
        rubrics.append({
            "id": rubric_id,
            "parent_id": _to_int(r.get("parent_id")),
            "name": r.get("name") or "",
            "alias": r.get("alias"),
            "is_primary": r.get("kind") == "primary",
        })

    contacts = []
    seen = set()
    for group in item.get("contact_groups") or []:
        for c in (group or {}).get("contacts") or []:
            if not isinstance(c, dict):
                continue
            c_type = (c.get("type") or "").lower()
            value = c.get("value") or c.get("url") or c.get("text")
            if not c_type or not value or (c_type, value) in seen:
                continue
            seen.add((c_type, value))
            contacts.append({
                "type": c_type,
                "value": str(value),
                "text": c.get("print_text") or c.get("text"),
                "url": c.get("url"),
                "comment": c.get("comment"),
                "position": len(contacts),
            })

    rubrics_names = [r["name"] for r in rubrics if r["name"]]
    primary_rubric = (
        next((r["name"] for r in rubrics if r["is_primary"] and r["name"]), None)
        or (rubrics_names[0] if rubrics_names else None)
        or item.get("purpose_name")
        or default_category
        or TYPE_CATEGORY.get(item.get("type"))
    )
    if primary_rubric and primary_rubric not in rubrics_names:
        rubrics_names.append(primary_rubric)

    return {
        "id": branch_id,
        "raw_id": raw_id,
        "org_id": org_id,
        "org": org,
        "region_id": _to_int(item.get("region_id")),
        "name": (
            item.get("name") or item.get("full_name") or name_ex.get("primary")
            or item.get("building_name") or item.get("purpose_name") or ""
        ),
        "name_primary": name_ex.get("primary"),
        "name_extension": name_ex.get("extension") or item.get("purpose_name"),
        "legal_name": name_ex.get("legal_name"),
        "address_name": item.get("address_name"),
        "full_address_name": item.get("full_address_name"),
        "address_comment": item.get("address_comment"),
        "postcode": address.get("postcode"),
        "building_id": _to_int(address.get("building_id")),
        "city": adm_div.get("city") or adm_div.get("settlement") or item.get("city_alias"),
        "district": adm_div.get("district"),
        "lat": point.get("lat"),
        "lon": point.get("lon"),
        "rating": reviews.get("general_rating") or reviews.get("rating"),
        "review_count": reviews.get("general_review_count") or reviews.get("review_count"),
        "org_rating": reviews.get("org_rating"),
        "org_review_count": reviews.get("org_review_count"),
        "primary_rubric": primary_rubric,
        "rubrics": rubrics,
        "rubrics_names": rubrics_names,
        "schedule": item.get("schedule"),
        "timezone": item.get("timezone"),
        "attributes": item.get("attribute_groups"),
        "flags": item.get("flags"),
        "contacts": contacts,
        "raw": item,
    }


def parse_review(review: Dict[str, Any], branch_id: int) -> Dict[str, Any]:
    user = review.get("user") or {}
    answer = review.get("official_answer") or {}
    user_name = user.get("name") or " ".join(
        p for p in (user.get("first_name"), user.get("last_name")) if p
    )
    return {
        "id": str(review["id"]),
        "branch_id": branch_id,
        "provider": review.get("provider"),
        "rating": review.get("rating"),
        "text": review.get("text"),
        "user_id": str(user["id"]) if user.get("id") is not None else None,
        "user_name": user_name or None,
        "user_reviews_count": user.get("reviews_count"),
        "likes_count": int(review.get("likes_count") or 0),
        "comments_count": int(review.get("comments_count") or 0),
        "photos_count": len(review.get("photos") or []),
        "is_verified": review.get("is_verified"),
        "is_hidden": review.get("is_hidden"),
        "hiding_reason": review.get("hiding_reason"),
        "official_answer_text": answer.get("text"),
        "official_answer_date": answer.get("date_created"),
        "date_created": review.get("date_created"),
        "date_edited": review.get("date_edited"),
        "url": review.get("url"),
        "raw": review,
    }

def parse_review_comment(comment: Dict[str, Any], review_id: str, branch_id: int) -> Dict[str, Any]:
    """Комментарий к отзыву: официальный ответ организации или реплика пользователя."""
    author = (comment.get("user") or {}).get("name") or (comment.get("org") or {}).get("name")
    return {
        "id": str(comment["id"]),
        "review_id": review_id,
        "branch_id": branch_id,
        "text": comment.get("text"),
        "is_official_answer": comment.get("is_official_answer"),
        "author_name": author,
        "date_created": comment.get("date_created"),
        "is_hidden": comment.get("is_hidden"),
        "raw": comment,
    }


# ---------------------------------------------------------------------------
# География: WKT-полигоны и проекция карты (Web Mercator, тайлы 256 px —
# проверено калибровкой по URL карты 2gis.kz: на 18-м зуме 5.364e-6° на пиксель)
# ---------------------------------------------------------------------------

Ring = List[Tuple[float, float]]


def wkt_rings(wkt: str) -> List[Ring]:
    """Все кольца POLYGON/MULTIPOLYGON как списки (lon, lat)."""
    return [
        [tuple(map(float, p.split())) for p in ring.split(",")]
        for ring in re.findall(r"\(([^()]+)\)", wkt or "")
    ]


def point_in_rings(x: float, y: float, rings: List[Ring]) -> bool:
    """Even-odd правило по всем кольцам: дыры полигона учитываются автоматически."""
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
    return inside


def lonlat_to_world(lon: float, lat: float, zoom: int) -> Tuple[float, float]:
    size = 256 * 2 ** zoom
    lat_r = math.radians(lat)
    return (lon + 180) / 360 * size, (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * size


def world_to_lonlat(x: float, y: float, zoom: int) -> Tuple[float, float]:
    size = 256 * 2 ** zoom
    return x / size * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))



def extract_initial_state(html_text: str, var: str = "initialState") -> Optional[dict]:
    """Достаёт `var <var> = JSON.parse('...')` из HTML страницы 2ГИС."""
    marker = f"var {var} = JSON.parse('"
    start = html_text.find(marker)
    if start == -1:
        return None
    i = start + len(marker)
    # содержимое JS-строки в одинарных кавычках -> настоящая строка (как её увидит JSON.parse)
    n = len(html_text)
    out = []
    while i < n:
        ch = html_text[i]
        if ch == "'":
            break
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = html_text[i + 1]
        if nxt == "u":
            out.append(chr(int(html_text[i + 2:i + 6], 16)))
            i += 6
        elif nxt == "x":
            out.append(chr(int(html_text[i + 2:i + 4], 16)))
            i += 4
        else:
            out.append(_JS_ESCAPES.get(nxt, nxt))
            i += 2
    try:
        return json.loads("".join(out))
    except json.JSONDecodeError as e:
        logger.error("Ошибка парсинга %s: %s (pos %s)", var, e, e.pos)
        return None


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def embedded_reviews(html_text: str) -> List[Dict[str, Any]]:
    """
    Первая страница отзывов (до 50), встроенная в `__REACT_QUERY_STATE__`.
    Блок есть, но не разобрался — исключение: иначе объект сочтётся «без отзывов».
    """
    state = extract_initial_state(html_text, "__REACT_QUERY_STATE__")
    if state is None:
        if "var __REACT_QUERY_STATE__" in html_text:
            raise RuntimeError("не удалось разобрать встроенные отзывы (__REACT_QUERY_STATE__)")
        return []
    reviews = []
    for query in state.get("queries") or []:
        if (query.get("queryKey") or [None])[0] != "fetchEntityReviews":
            continue
        for pg in ((query.get("state") or {}).get("data") or {}).get("pages") or []:
            reviews.extend(r for r in (pg or {}).get("items") or [] if isinstance(r, dict) and r.get("id"))
    return reviews


def state_entities(state: Optional[dict]) -> Dict[str, Dict[str, Any]]:
    """id -> данные сущностей, встроенных в страницу."""
    profile = (((state or {}).get("data") or {}).get("entity") or {}).get("profile") or {}
    return {str(k): (v or {}).get("data") or {} for k, v in profile.items()}


def search_meta(state: Optional[dict]) -> Tuple[Optional[int], Optional[int], List[str]]:
    """(total, pages, id объектов первой страницы) поисковой выдачи."""
    search = ((state or {}).get("data") or {}).get("search") or {}
    total = pages = None
    for v in (search.get("profile") or {}).values():
        d = (v or {}).get("data") or {}
        total, pages = d.get("total"), d.get("pages")
        break
    first_ids: List[str] = []
    for v in (search.get("pagination") or {}).values():
        first_ids = [str(i) for i in (((v or {}).get("1") or {}).get("data") or [])]
        break
    return total, pages, first_ids


def stop_from_item(item: Dict[str, Any], city_slug: str, region: str, city: str) -> Optional[Dict[str, Any]]:
    """Остановка/станция любого транспорта (автобус, метро, троллейбус, трамвай)."""
    point = item.get("point")
    subtype = item.get("subtype")
    if not point or item.get("type") != "station":
        return None
    adm = {a.get("type"): (a.get("name") or "").replace("\xa0", " ")
           for a in item.get("adm_div") or [] if isinstance(a, dict)}
    district = adm.get("district")
    routes = [
        {
            "id": str(_to_int(r.get("id"))),
            "name": str(r.get("name") or ""),
            "subtype": r.get("subtype") or subtype or "bus",
            "from_name": r.get("from_name"),
            "to_name": r.get("to_name"),
            "color": r.get("color"),
        }
        for r in item.get("routes") or []
        if isinstance(r, dict)
    ]
    if not subtype and routes:
        subtype = routes[0]["subtype"]
    return {
        "id": str(_to_int(item.get("id"))),
        "name": item.get("name") or item.get("full_name") or "",
        "type": "station",
        "subtype": subtype or "stop",
        "lat": point.get("lat"),
        "lon": point.get("lon"),
        "district": district,
        "region": adm.get("region") or region,
        "city": adm.get("city") or adm.get("settlement") or city,  # реальный населённый пункт остановки
        "city_slug": city_slug,
        "routes": routes,
        "raw": item,
    }


def route_from_item(item: Dict[str, Any], city_slug: str) -> Dict[str, Any]:
    return {
        "id": str(_to_int(item.get("id"))),
        "city_slug": city_slug,
        "name": str(item.get("name") or ""),
        "subtype": item.get("subtype") or "bus",
        "from_name": item.get("from_name"),
        "to_name": item.get("to_name"),
        "color": item.get("color"),
        "raw": item,
    }


def build_route_stop_rows(
    region: str, city_name: str, city_slug: str, stops: List[Dict[str, Any]], subtypes: Tuple[str, ...]
) -> List[Dict[str, Any]]:
    """Маршрут x остановка (формат 2gis_bus_stations)."""
    want_all = "all" in subtypes
    rows = []
    for s in stops:
        for r in s.get("routes", []):
            st = (r.get("subtype") or "bus").lower()
            if not want_all and st not in subtypes:
                continue
            rows.append({
                "region": region,
                "city": city_name,
                "city_slug": city_slug,
                "route_id": str(r.get("id")),
                "route_number": str(r.get("name") or ""),
                "route_subtype": st,
                "route_from": r.get("from_name"),
                "route_to": r.get("to_name"),
                "stop_id": str(s.get("id")),
                "stop_name": s.get("name"),
                "lat": s.get("lat"),
                "lon": s.get("lon"),
                "district": s.get("district"),
                "color": r.get("color"),
            })
    return rows


def save_csv(rows: List[Dict[str, Any]], out_path: Path) -> None:
    if not rows:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({
                k: json.dumps(r.get(k), ensure_ascii=False) if isinstance(r.get(k), (dict, list)) else r.get(k)
                for k in fieldnames
            })
    logger.info("CSV: %s (%d строк)", out_path, len(rows))


# ---------------------------------------------------------------------------
# Браузер
# ---------------------------------------------------------------------------

class CaptchaBlockedError(RuntimeError):
    """2ГИС показал капчу, и её не решили за CAPTCHA_WAIT_SEC — обход останавливается."""


class NetworkDownError(RuntimeError):
    """Сети нет дольше NET_WAIT_MAX_SEC — обход останавливается, прогресс сохранён."""


def _looks_like_captcha(html: str) -> bool:
    return "g-recaptcha" in html or "2GIS Captcha" in html


class DgisBrowserScraper:
    """
    Системный Chrome через Playwright. По умолчанию окно видимое: 2ГИС отдаёт
    капчу headless-браузерам и дата-центровым IP.
    """

    def __init__(self, headless: bool = False, channel: Optional[str] = "chrome",
                 proxies: Optional[List[str]] = None, map_mode: bool = False):
        self.headless = headless
        self.channel = channel
        self.map_mode = map_mode  # скан карты района требует WebGL; остальным режимам GPU не нужен
        self.proxies = [p.strip() for p in proxies or [] if p.strip()]
        self._proxy_idx = 0
        self._actions = 0
        self._loads: Dict[int, int] = {}
        self._pw = None
        self._browser = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        args = CHROME_ARGS if self.map_mode else CHROME_ARGS + ["--disable-gpu"]
        launch_kwargs: Dict[str, Any] = {"headless": self.headless, "args": args}
        if self.channel and self.channel != "chromium":
            launch_kwargs["channel"] = self.channel
        self._browser = self._pw.chromium.launch(**launch_kwargs)
        logger.info("Браузер запущен (headless=%s, channel=%s, прокси: %d)",
                    self.headless, self.channel, len(self.proxies))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def new_page(self, viewport: Optional[Dict[str, int]] = None, with_map: bool = False) -> Page:
        """
        Новый контекст (свои куки) со случайным (или заданным) viewport и следующим прокси.
        Трекеры, картинки, шрифты и медиа не грузятся; тайлы карты — только при with_map
        (нужны для скана района). Данные 2ГИС (HTML, XHR каталога и отзывов) не трогаются.
        """
        kwargs: Dict[str, Any] = {"viewport": viewport or random.choice(VIEWPORTS), "locale": "ru-RU"}
        if self.proxies:
            kwargs["proxy"] = {"server": self.proxies[self._proxy_idx % len(self.proxies)]}
            self._proxy_idx += 1
        context = self._browser.new_context(**kwargs)
        blocked_hosts = TRACKER_HOSTS if with_map else TRACKER_AND_MAP_HOSTS

        def route(r) -> None:
            req = r.request
            if blocked_hosts.search(req.url) or (not with_map and req.resource_type in BLOCKED_RESOURCE_TYPES):
                r.abort()
            else:
                r.continue_()

        context.route("**/*", route)
        page = context.new_page()
        page._dgis_with_map = with_map  # для пересоздания с теми же настройками
        return page

    def recycle(self, page: Page) -> Page:
        """Каждые RECYCLE_EVERY загрузок пересоздаёт контекст: Chrome копит память (кэш SPA и карты)."""
        if self._loads.get(id(page), 0) < RECYCLE_EVERY:
            return page
        self._loads.pop(id(page), None)
        viewport = page.viewport_size
        with_map = getattr(page, "_dgis_with_map", False)
        page.context.close()
        return self.new_page(viewport, with_map)

    def pace(self, bounds: Tuple[float, float] = COOLDOWN) -> None:
        """Пауза после действия; каждые LONG_BREAK_EVERY действий — длинный перерыв."""
        self._actions += 1
        if self._actions % LONG_BREAK_EVERY == 0:
            pause = random.uniform(*LONG_BREAK)
            logger.info("Антибан: перерыв %.0f сек после %d действий", pause, self._actions)
            time.sleep(pause)
        else:
            _pause(bounds)

    def load_page(self, page: Page, url: str, retries: int = 3) -> Optional[str]:
        """
        Открывает страницу; при капче ждёт, пока её решат в окне браузера.
        Пропала сеть — ждёт её возвращения (до NET_WAIT_MAX_SEC), не расходуя попытки.
        """
        self._loads[id(page)] = self._loads.get(id(page), 0) + 1
        attempt = 0
        net_wait_until: Optional[float] = None
        while attempt < retries:
            try:
                page.goto(url, wait_until="commit", timeout=45000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                if _looks_like_captcha(page.content()):
                    self._wait_captcha(page, url)
                accept = page.locator("button#acceptRiskButton")
                if accept.count():
                    accept.first.click(timeout=5000)
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                if net_wait_until is not None:
                    logger.info("Сеть восстановлена, продолжаем.")
                return page.content()
            except (CaptchaBlockedError, NetworkDownError):
                raise
            except Exception as e:
                if NETWORK_ERROR.search(str(e)):
                    if net_wait_until is None:
                        net_wait_until = time.monotonic() + NET_WAIT_MAX_SEC
                        logger.warning("Нет сети (%s) — ждём восстановления до %d мин",
                                       NETWORK_ERROR.search(str(e)).group(0), NET_WAIT_MAX_SEC // 60)
                    if time.monotonic() < net_wait_until:
                        time.sleep(NET_RETRY_SEC)
                        continue
                    raise NetworkDownError(f"Сети нет дольше {NET_WAIT_MAX_SEC // 60} мин") from e
                attempt += 1
                logger.warning("Ошибка загрузки %s: %s (попытка %d/%d)", url, e, attempt, retries)
                _pause(COOLDOWN)
        return None

    def load_html(self, page: Page, url: str) -> Tuple[Optional[str], str]:
        """
        Загрузка карточки: HTML (там уже весь initialState) и итоговый URL после редиректов.
        Через load_page — со всеми таймаутами: чтение тела ответа без таймаута однажды зависло на 15 минут.
        """
        html = self.load_page(page, url)
        return html, page.url

    def _wait_captcha(self, page: Page, url: str) -> str:
        logger.warning("2ГИС показал капчу на %s — решите её в окне браузера (ждём %d сек)",
                       url, CAPTCHA_WAIT_SEC)
        deadline = time.monotonic() + CAPTCHA_WAIT_SEC
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            html = page.content()
            if not _looks_like_captcha(html):
                logger.info("Капча пройдена, продолжаем.")
                return html
        raise CaptchaBlockedError(
            "Капча не решена. Обход остановлен, прогресс сохранён — запустите ту же команду позже "
            "(или с другого IP / через --proxies)."
        )


# ---------------------------------------------------------------------------
# Краулер
# ---------------------------------------------------------------------------

@dataclass
class City:
    region: str
    name: str
    slug: str


@dataclass
class SearchRun:
    items: List[Dict[str, Any]]
    total: Optional[int]
    pages_total: int
    pages_done: int

    ended: bool = False  # выдача кончилась раньше заявленного числа страниц

    @property
    def complete(self) -> bool:
        return self.ended or self.pages_done >= self.pages_total

    @property
    def unique(self) -> int:
        return len({str(i.get("id")) for i in self.items})


def _items_listener(sink: Callable[[Dict[str, List[str]], Dict[str, Any]], None]) -> Callable[[Response], None]:
    """Обработчик ответов сайта: отдаёт в sink (query-параметры, result) каждого /3.0/items."""
    def on_response(resp: Response) -> None:
        parsed = urllib.parse.urlparse(resp.url)
        if not parsed.path.endswith("/3.0/items") or resp.status != 200:
            return
        try:
            result = resp.json().get("result") or {}
        except Exception:
            return
        sink(urllib.parse.parse_qs(parsed.query), result)
    return on_response


def _wait_for(page: Page, condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        page.wait_for_timeout(250)
    return condition()


class DgisCrawler:
    def __init__(self, scraper: DgisBrowserScraper, storage: SyncStorage, out_dir: str = "./output"):
        self.scraper = scraper
        self.storage = storage
        self.out_dir = Path(out_dir)
        self._region_ids: Dict[str, str] = {}  # slug города -> region_id 2ГИС (город + пригороды)

    def in_city(self, city: City, items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Отбрасывает объекты чужих регионов: когда своя выдача кончается, 2ГИС дописывает
        результаты из других городов. region_id города берётся с объекта, у которого
        city_alias совпадает со slug города (у пригородов Астаны свой alias, но тот же region_id).
        """
        items = list(items)
        rid = self._region_ids.get(city.slug)
        if rid is None:
            rid = next((str(i["region_id"]) for i in items
                        if i.get("city_alias") == city.slug and i.get("region_id") is not None), None)
            if rid is None:
                return items  # регион ещё не определён — ничего не отбрасываем
            self._region_ids[city.slug] = rid
        return [i for i in items if i.get("region_id") is None or str(i["region_id"]) == rid]


    # ------------------------------------------------------------------ поисковая выдача

    def paginate_search(self, page: Page, url: str, max_pages: int,
                        heartbeat: Optional[Callable[[], None]] = None) -> SearchRun:
        """
        Проходит все страницы выдачи кликами. Страница 1 — из initialState,
        остальные — из XHR /3.0/items, который сайт запрашивает при клике.
        """
        by_page: Dict[int, List[Dict[str, Any]]] = {}

        def sink(qs: Dict[str, List[str]], result: Dict[str, Any]) -> None:
            if "building_id" in qs:
                return
            try:
                by_page[int(qs.get("page", ["1"])[0])] = result.get("items") or []
            except ValueError:
                return

        listener = _items_listener(sink)
        page.on("response", listener)
        try:
            html = self.scraper.load_page(page, url)
            state = extract_initial_state(html or "")
            if state is None:
                # страница не загрузилась или не разобралась — это не «0 результатов»
                logger.warning("Страница выдачи не разобрана: %s", url)
                return SearchRun([], None, 1, 0)
            entities = state_entities(state)
            total, pages, first_ids = search_meta(state)
            if total is None:
                # запрос открыл одну карточку напрямую (точное совпадение)
                items = list(entities.values())
                return SearchRun(items, len(items), 1, 1)
            items = [entities[i] for i in first_ids if i in entities]
            pages_total = max(1, pages or 1)
            pages_done = 1
            last_len = len(items)
            for page_no in range(2, min(pages_total, max_pages) + 1):
                self.scraper.pace()
                got = self._click_page(page, page_no, by_page)
                if got is None:
                    if last_len < SEARCH_PAGE_SIZE:
                        # неполная предыдущая страница и дальше ссылок нет — выдача кончилась,
                        # хотя 2ГИС заявлял больше страниц (счётчик у него бывает завышен)
                        logger.info("Выдача закончилась на стр. %d (2ГИС заявлял %d): %s",
                                    page_no - 1, pages_total, url)
                        return SearchRun(items, total, pages_total, pages_done, ended=True)
                    logger.warning("Страница %d/%d не получена (%s)", page_no, pages_total, url)
                    break
                items.extend(got)
                last_len = len(got)
                pages_done = page_no
                if heartbeat and page_no % 20 == 0:
                    heartbeat()
            return SearchRun(items, total, pages_total, pages_done)
        finally:
            page.remove_listener("response", listener)

    def _click_page(self, page: Page, page_no: int, by_page: Dict[int, List[Dict[str, Any]]]
                    ) -> Optional[List[Dict[str, Any]]]:
        link = page.locator(f"a[href$='/page/{page_no}'], a[href*='/page/{page_no}?']").first
        for _ in range(2):
            by_page.pop(page_no, None)
            try:
                link.wait_for(state="attached", timeout=8000)
                link.scroll_into_view_if_needed(timeout=3000)
                link.click(timeout=5000)
            except Exception as e:
                logger.debug("Клик по странице %d не удался: %s", page_no, e)
                continue
            if _wait_for(page, lambda: page_no in by_page, XHR_WAIT_SEC):
                return by_page[page_no]
        if _looks_like_captcha(page.content()):
            self.scraper._wait_captcha(page, page.url)
        return None

    # ------------------------------------------------------------------ здание / ЖК

    def crawl_building(self, page: Page, city: City, building_id: str
                       ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
        """
        Карточка здания + всё, что 2ГИС показывает на вкладке «В здании».
        Возвращает (сущности со страницы, объекты «В здании», сколько их заявил 2ГИС).
        Здание с одной организацией сайт открывает как карточку этой организации (/firm/…):
        тогда сохраняется она, а вкладка «В здании» ищется уже на её странице.
        """
        items: Dict[str, Dict[str, Any]] = {}
        totals: List[int] = []

        def sink(qs: Dict[str, List[str]], result: Dict[str, Any]) -> None:
            if qs.get("building_id", [None])[0] != building_id:
                return
            if result.get("total") is not None:
                totals.append(int(result["total"]))
            for it in result.get("items") or []:
                items[str(_to_int(it.get("id")))] = it

        listener = _items_listener(sink)
        page.on("response", listener)
        try:
            html = self.scraper.load_page(page, f"{SITE}/{city.slug}/geo/{building_id}")
            entities = state_entities(extract_initial_state(html or ""))
            if not entities:
                raise RuntimeError("карточка здания не загрузилась или не разобралась")
            on_page = list(entities.values())
            tab = page.locator("a[href*='/tab/inside']")
            if tab.count() == 0:
                return on_page, [], 0  # в здании нет организаций
            tab.first.click(timeout=8000)
            if not _wait_for(page, lambda: bool(totals), XHR_WAIT_SEC):
                raise RuntimeError("вкладка «В здании» не вернула список")
            total = totals[0]
            self._load_more(page, lambda: len(items), total)
            return on_page, list(items.values()), total
        finally:
            page.remove_listener("response", listener)

    def _load_more(self, page: Page, count: Callable[[], int], want: Optional[int]) -> bool:
        """
        Жмёт «Загрузить ещё», пока растёт count() и не достигнут want (None — до конца ленты).
        True — лента закончилась сама (кнопки нет) или достигнут want; False — догрузка застряла.
        """
        button = page.locator("button", has_text=LOAD_MORE).first
        misses = 0
        while want is None or count() < want:
            if misses >= 2:
                return False
            try:
                button.wait_for(state="visible", timeout=3000)
            except Exception:
                return True  # кнопки нет — лента закончилась
            before = count()
            try:
                button.scroll_into_view_if_needed(timeout=3000)
                button.click(timeout=3000)
            except Exception:
                try:
                    button.evaluate("b => b.click()", timeout=3000)  # перекрыта плавающим блоком карты
                except Exception:
                    pass  # кнопка перерисовалась — следующая итерация найдёт новую
            misses = 0 if _wait_for(page, lambda: count() > before, XHR_WAIT_SEC) else misses + 1
            self.scraper.pace(SHORT_COOLDOWN)
        return True

    # ------------------------------------------------------------------ каталог

    def discover_rubrics(self, page: Page, city: City) -> List[Tuple[str, str]]:
        """Рубрики из рубрикатора города: /rubrics -> разделы -> /search/<имя>/rubricId/<id>."""
        html = self.scraper.load_page(page, f"{SITE}/{city.slug}/rubrics")
        if not html:
            return []
        metas = list(dict.fromkeys(re.findall(rf'href="/{city.slug}/rubrics/subrubrics/(\d+)"', html)))
        found: Dict[str, str] = {}
        for meta_id in metas:
            self.scraper.pace()
            sub = self.scraper.load_page(page, f"{SITE}/{city.slug}/rubrics/subrubrics/{meta_id}")
            for raw_name, rid in re.findall(rf'href="/{city.slug}/search/([^"/]+)/rubricId/(\d+)"', sub or ""):
                found.setdefault(rid, urllib.parse.unquote(raw_name))
        logger.info("Рубрикатор %s: разделов %d, рубрик %d", city.slug, len(metas), len(found))
        return list(found.items())

    def _ingest(self, city: City, items: Iterable[Dict[str, Any]], default_category: Optional[str],
                expand: bool) -> int:
        """Сохраняет объекты и ставит в очередь найденные в них рубрики и здания."""
        objects: Dict[int, Dict[str, Any]] = {}
        rubrics: Dict[str, str] = {}
        buildings: Dict[str, Optional[str]] = {}
        for item in self.in_city(city, items):
            itype = item.get("type")
            if itype not in OBJECT_TYPES:
                continue
            if itype == "adm_div" and item.get("subtype") in SKIP_ADM_SUBTYPES:
                continue
            obj = parse_branch(item, default_category)
            if obj is None:
                continue
            objects[obj["id"]] = obj
            if not expand:
                continue
            for r in obj["rubrics"]:
                rubrics.setdefault(str(r["id"]), r["name"])
            if itype == "building":
                buildings[str(obj["id"])] = obj["name"]  # числовой id: '<id>_<хэш>' ломает сверку «В здании»
            elif obj["building_id"]:
                buildings.setdefault(str(obj["building_id"]), obj["address_name"])
        self.storage.save_branches(list(objects.values()))
        self.storage.add_web_tasks(city.slug, "rubric", rubrics.items())
        self.storage.add_web_tasks(city.slug, "building", buildings.items())
        return len(objects)

    def crawl_catalog(
        self,
        city: City,
        queries: Optional[List[str]] = None,
        expand: bool = True,
        buildings: bool = True,
        max_pages: int = 10_000,
        max_tasks: Optional[int] = None,
        fresh: bool = False,
    ) -> None:
        """
        Обход через очередь в БД. Семена: рубрикатор города + SEED_QUERIES
        (или только `queries`). expand=True — дособирать рубрики и здания,
        найденные в карточках, пока очередь не опустеет.
        """
        if fresh:
            logger.info("Очередь %s очищена: %d задач", city.slug, self.storage.clear_web_tasks(city.slug))
        reset = self.storage.reset_web_tasks(city.slug, MAX_TASK_ATTEMPTS)
        if reset:
            logger.info("Возвращено в очередь незавершённых задач: %d", reset)

        page = self.scraper.new_page()
        try:
            if queries:
                self.storage.add_web_tasks(city.slug, "query", ((q, q) for q in queries))
            else:
                self.storage.add_web_tasks(city.slug, "rubric", self.discover_rubrics(page, city))
                self.storage.add_web_tasks(city.slug, "query", ((q, q) for q in SEED_QUERIES))

            kinds = ["query", "rubric"] + (["building"] if buildings else [])
            done = 0
            while max_tasks is None or done < max_tasks:
                page = self.scraper.recycle(page)
                task = self.storage.claim_web_task(city.slug, kinds)
                if task is None:
                    break
                try:
                    self._run_task(page, city, task, expand, max_pages)
                except (CaptchaBlockedError, NetworkDownError) as e:
                    self.storage.finish_web_task(task["id"], "pending", error=str(e))
                    logger.error("%s", e)
                    break
                except KeyboardInterrupt:
                    self.storage.finish_web_task(task["id"], "pending", error="прервано вручную")
                    raise
                except Exception as e:
                    self.storage.finish_web_task(task["id"], "error", error=str(e)[:1000])
                    logger.error("Задача %s %s: %s", task["kind"], task["label"] or task["key"], e)
                done += 1
                self.scraper.pace()
        finally:
            page.context.close()

    def _run_task(self, page: Page, city: City, task: Dict[str, Any], expand: bool, max_pages: int
                  ) -> List[Dict[str, Any]]:
        """Выполняет задачу очереди и возвращает собранные сырые объекты."""
        kind, key, label = task["kind"], task["key"], task["label"]
        if kind == "building":
            on_page, items, total = self.crawl_building(page, city, key)
            everything = on_page + items
            saved = self._ingest(city, everything, None, expand)
            collected = len({str(_to_int(i.get("id"))) for i in items})
            status = "done" if collected >= total else "incomplete"
            self.storage.finish_web_task(task["id"], status, total=total, collected=collected)
            logger.info("[здание] %s: в здании %d/%d, сохранено %d%s",
                        label or key, collected, total, saved, "" if status == "done" else "  НЕПОЛНО")
            return everything

        if kind == "rubric":
            url = f"{SITE}/{city.slug}/search/{urllib.parse.quote(label or 'рубрика')}/rubricId/{key}"
        else:
            url = f"{SITE}/{city.slug}/search/{urllib.parse.quote(key)}"
        run = self.paginate_search(page, url, max_pages, heartbeat=lambda: self.storage.touch_web_task(task["id"]))
        saved = self._ingest(city, run.items, label if kind == "rubric" else key, expand)
        status = "done" if run.complete else "incomplete"
        self.storage.finish_web_task(task["id"], status, total=run.total, collected=run.unique,
                                     pages_total=run.pages_total, pages_done=run.pages_done)
        logger.info("[%s] %s: собрано %d из %s (стр. %d/%d), сохранено объектов %d%s",
                    "рубрика" if kind == "rubric" else "запрос", label or key, run.unique, run.total,
                    run.pages_done, run.pages_total, saved, "" if status == "done" else "  НЕПОЛНО")
        return run.items

    # ------------------------------------------------------------------ полные карточки

    def fetch_card(self, page: Page, city: City, obj_id: int, obj_type: str
                   ) -> Tuple[Optional[Dict[str, Any]], Optional[int]]:
        """
        Полная карточка со страницы объекта: контакты, соцсети, атрибуты — всё, что отдаёт сайт.
        Здание с одной организацией сайт перенаправляет на карточку этой организации —
        тогда возвращается карточка организации и её id.
        """
        section = "firm" if obj_type == "branch" else "geo"
        html, final_url = self.scraper.load_html(page, f"{SITE}/{city.slug}/{section}/{obj_id}")
        entities = state_entities(extract_initial_state(html or ""))
        if str(obj_id) in entities:
            return entities[str(obj_id)], obj_id
        m = re.search(r"/(?:firm|geo)/(\d+)", final_url or "")
        if m and m.group(1) in entities:
            return entities[m.group(1)], int(m.group(1))
        return None, None

    def enrich_cards(self, city: City, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Открывает карточку каждого объекта и сохраняет её целиком. Возвращает разобранные карточки."""
        cards = []
        page = self.scraper.new_page()
        try:
            for idx, row in enumerate(rows, 1):
                page = self.scraper.recycle(page)
                entity, got_id = self.fetch_card(page, city, int(row["id"]), row["type"])
                card = parse_branch(entity) if entity else None
                if card is None:
                    logger.warning("Карточка %s (%s) не получена", row["id"], row.get("name"))
                    self.storage.mark_card_failed(int(row["id"]))
                    self.scraper.pace()
                    continue
                self.storage.save_branches([card], full_card=True)
                if got_id != int(row["id"]):
                    # у здания нет своей карточки — сайт показывает единственную организацию в нём
                    self.storage.mark_card_synced([int(row["id"])])
                cards.append(card)
                logger.info("[карточка %d/%d] %s: контактов %d, рубрик %d%s",
                            idx, len(rows), card["name"], len(card["contacts"]), len(card["rubrics"]),
                            "" if got_id == int(row["id"]) else f" (перенаправлено с {row['id']})")
                self.scraper.pace()
        except (CaptchaBlockedError, NetworkDownError) as e:
            logger.error("%s", e)
        finally:
            page.context.close()
        return cards

    # ------------------------------------------------------------------ район

    def resolve_area(self, page: Page, city: City, ref: str) -> Dict[str, Any]:
        """Район/микрорайон/жилмассив по ID или названию: полигон и число зданий по данным 2ГИС."""
        area_id = ref if ref.isdigit() else None
        if area_id is None:
            html = self.scraper.load_page(page, f"{SITE}/{city.slug}/search/{urllib.parse.quote(ref)}")
            candidates = [
                (k, d) for k, d in state_entities(extract_initial_state(html or "")).items()
                if d.get("type") == "adm_div" and d.get("subtype") not in SKIP_ADM_SUBTYPES - {"district"}
            ]
            exact = [k for k, d in candidates if (d.get("name") or "").lower() == ref.lower()]
            if not (exact or candidates):
                raise SystemExit(f"Район «{ref}» не найден в {city.name}; укажите ID из URL 2ГИС (/geo/<id>)")
            area_id = exact[0] if exact else candidates[0][0]
        html = self.scraper.load_page(page, f"{SITE}/{city.slug}/geo/{area_id}")
        entity = state_entities(extract_initial_state(html or "")).get(area_id) or {}
        rings = wkt_rings((entity.get("geometry") or {}).get("selection"))
        if not rings:
            raise SystemExit(f"У объекта {area_id} нет границ — это не район")
        return {
            "id": area_id,
            "name": entity.get("name") or ref,
            "rings": rings,
            "building_count": (entity.get("statistics") or {}).get("building_count"),
        }

    def scan_area(self, page: Page, city: City, rings: List[Ring]) -> Dict[str, Dict[str, Any]]:
        """
        Прокликивает карту сеткой внутри полигона на 18-м зуме. Клик по карте
        открывает объект под курсором (здание, организация, площадка, остановка);
        сайт при этом запрашивает /3.0/items/byid — из этого ответа берётся объект.
        """
        x0, y0, x1, y1 = SCAN_RECT
        world_rings = [[lonlat_to_world(lon, lat, SCAN_ZOOM) for lon, lat in ring] for ring in rings]
        xs = [x for ring in world_rings for x, _ in ring]
        ys = [y for ring in world_rings for _, y in ring]
        points = [
            (x, y)
            for x in _frange(min(xs), max(xs), SCAN_STEP_PX)
            for y in _frange(min(ys), max(ys), SCAN_STEP_PX)
            if point_in_rings(x, y, world_rings)
        ]
        views: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
        for x, y in points:
            views.setdefault((int((x - min(xs)) // (x1 - x0)), int((y - min(ys)) // (y1 - y0))), []).append((x, y))
        logger.info("Скан карты: точек %d (шаг %.0f м), экранов %d",
                    len(points), SCAN_STEP_PX * 40075016.7 * math.cos(math.radians(rings[0][0][1]))
                    / (256 * 2 ** SCAN_ZOOM), len(views))

        found: Dict[str, Dict[str, Any]] = {}
        responses: List[int] = [0]

        def on_response(resp: Response) -> None:
            if not urllib.parse.urlparse(resp.url).path.endswith("/3.0/items/byid") or resp.status != 200:
                return
            try:
                items = (resp.json().get("result") or {}).get("items") or []
            except Exception:
                return
            for it in items:
                found[str(_to_int(it.get("id")))] = it  # у точечных объектов id вида '<id>_<хэш клика>'
            responses[0] += 1

        page.on("response", on_response)
        try:
            for n, ((col, row), pts) in enumerate(sorted(views.items()), 1):
                vx0 = min(xs) + col * (x1 - x0)
                vy0 = min(ys) + row * (y1 - y0)
                cx = vx0 - x0 + SCAN_VIEWPORT["width"] / 2
                cy = vy0 - y0 + SCAN_VIEWPORT["height"] / 2
                lon, lat = world_to_lonlat(cx, cy, SCAN_ZOOM)
                self.scraper.load_page(page, f"{SITE}/{city.slug}?m={lon:.6f}%2C{lat:.6f}%2F{SCAN_ZOOM}")
                page.wait_for_timeout(2500)  # дорисовка тайлов
                before = len(found)
                for x, y in pts:
                    seen = responses[0]
                    page.mouse.click(x0 + (x - vx0), y0 + (y - vy0))
                    _wait_for(page, lambda: responses[0] > seen, 1.5)
                    _pause(SCAN_CLICK_PAUSE)
                logger.info("Экран %d/%d: кликов %d, новых объектов %d, всего %d",
                            n, len(views), len(pts), len(found) - before, len(found))
                self.scraper.pace()
        finally:
            page.remove_listener("response", on_response)
        return found

    def crawl_area(self, city: City, ref: str, with_reviews: bool = True, rescan: bool = False) -> None:
        """
        Все объекты района: скан карты -> полные карточки -> «В здании» у каждого
        здания -> полные карточки найденного внутри -> отзывы с комментариями.
        Полнота зданий сверяется со statistics.building_count из 2ГИС.
        """
        page = self.scraper.new_page(SCAN_VIEWPORT, with_map=True)
        try:
            area = self.resolve_area(page, city, ref)
            logger.info("Район: %s (id %s), зданий по данным 2ГИС: %s",
                        area["name"], area["id"], area["building_count"])
            self.storage.add_web_tasks(city.slug, "area", [(area["id"], area["name"])])
            cache = self.out_dir / f"scan_{city.slug}_{area['id']}.json"
            if cache.exists() and not rescan:
                scanned = json.loads(cache.read_text(encoding="utf-8"))
                logger.info("Скан района взят из %s (%d объектов); --rescan — сканировать заново",
                            cache, len(scanned))
            else:
                scanned = self.scan_area(page, city, area["rings"])
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(scanned, ensure_ascii=False), encoding="utf-8")
        finally:
            page.context.close()

        objects = {
            k: v for k, v in scanned.items()
            if v.get("type") in OBJECT_TYPES
            and not (v.get("type") == "adm_div" and v.get("subtype") in SKIP_ADM_SUBTYPES | {"living_area"})
        }
        stops = [s for s in (stop_from_item(v, city.slug, city.region, city.name) for v in scanned.values()) if s]
        self.storage.save_transport_stops(stops)
        self._ingest(city, objects.values(), None, expand=True)
        area_ids = {_to_int(k) for k in objects}
        logger.info("Скан: объектов %d, остановок %d", len(area_ids), len(stops))

        # полные карточки (из них же — building_id организаций)
        cards = self.enrich_cards(city, self.storage.branches_without_card(list(area_ids)))
        buildings = {
            str(r["id"]) for r in self.storage.branches_by_ids(list(area_ids))
            if r["type"] == "building"
        } | {
            str(c["building_id"]) for c in cards if c.get("building_id")
        }

        # «В здании» у каждого здания района
        page = self.scraper.new_page()
        try:
            self.storage.add_web_tasks(city.slug, "building", ((b, None) for b in buildings))
            for bid in sorted(buildings):
                page = self.scraper.recycle(page)
                task = self.storage.claim_web_task_by_key(city.slug, "building", bid, MAX_TASK_ATTEMPTS)
                if task is None:
                    continue  # уже собрано раньше
                try:
                    self._run_task(page, city, task, expand=True, max_pages=0)
                except (CaptchaBlockedError, NetworkDownError) as e:
                    self.storage.finish_web_task(task["id"], "pending", error=str(e))
                    logger.error("%s", e)
                    return
                except KeyboardInterrupt:
                    self.storage.finish_web_task(task["id"], "pending", error="прервано вручную")
                    raise
                except Exception as e:
                    self.storage.finish_web_task(task["id"], "error", error=str(e)[:1000])
                    logger.error("Здание %s: %s", bid, e)
                self.scraper.pace()
        finally:
            page.context.close()
        area_ids |= set(self.storage.branch_ids_in_buildings([_to_int(b) for b in buildings]))
        area_ids |= {_to_int(b) for b in buildings}
        area_ids.discard(None)

        self.enrich_cards(city, self.storage.branches_without_card(list(area_ids)))

        rows = self.storage.branches_by_ids(list(area_ids))
        in_polygon = sum(
            1 for r in rows
            if r["type"] == "building" and r["lon"] is not None
            and point_in_rings(r["lon"], r["lat"], area["rings"])
        )
        total = area["building_count"]
        self.storage.set_web_task_result(
            city.slug, "area", area["id"],
            "done" if total is None or in_polygon >= total else "incomplete", total, in_polygon,
        )
        logger.info("Район %s: объектов %d, зданий в границах %d из %s по данным 2ГИС",
                    area["name"], len(rows), in_polygon, area["building_count"])

        if with_reviews:
            pending = [r for r in rows if (r["review_count"] or 0) > 0 and r["reviews_synced_at"] is None]
            logger.info("Объектов с несобранными отзывами: %d", len(pending))
            self.crawl_reviews(city, pending)

    # ------------------------------------------------------------------ транспорт

    def crawl_transport(self, city: City, subtypes: Tuple[str, ...], max_pages: int = 10_000) -> None:
        want_all = "all" in subtypes
        queries = list(dict.fromkeys(
            q for st, qs in TRANSPORT_QUERIES.items() if want_all or st in subtypes for q in qs
        ))
        stops: Dict[str, Dict[str, Any]] = {}
        routes: Dict[str, Dict[str, Any]] = {}
        page = self.scraper.new_page()
        try:
            for q in queries:
                url = f"{SITE}/{city.slug}/search/{urllib.parse.quote(q)}"
                self.storage.add_web_tasks(city.slug, "transport", [(q, q)])
                items: List[Dict[str, Any]] = []
                for attempt in range(1, MAX_TASK_ATTEMPTS + 1):
                    page = self.scraper.recycle(page)
                    run = self.paginate_search(page, url, max_pages)
                    items.extend(run.items)
                    unique = len({str(_to_int(i.get("id"))) for i in items})
                    logger.info("[транспорт] %s: собрано %d из %s (стр. %d/%d), попытка %d%s",
                                q, unique, run.total, run.pages_done, run.pages_total, attempt,
                                "" if run.complete else "  НЕПОЛНО")
                    self.scraper.pace()
                    if run.complete:
                        break
                self.storage.set_web_task_result(city.slug, "transport", q,
                                                 "done" if run.complete else "incomplete", run.total, unique)
                for item in self.in_city(city, items):
                    if item.get("type") == "station":
                        stop = stop_from_item(item, city.slug, city.region, city.name)
                        if stop:
                            stops[stop["id"]] = stop
                    elif item.get("type") == "route":
                        route = route_from_item(item, city.slug)
                        routes[route["id"]] = route
        finally:
            page.context.close()

        for stop in stops.values():
            for r in stop["routes"]:
                routes.setdefault(r["id"], {**r, "city_slug": city.slug, "raw": r})
        routes = {k: v for k, v in routes.items() if want_all or (v.get("subtype") or "bus") in subtypes}
        rows = build_route_stop_rows(city.region, city.name, city.slug, list(stops.values()), subtypes)
        self.storage.save_transport_stops(list(stops.values()))
        self.storage.save_transport_routes(list(routes.values()))
        self.storage.save_transport_route_stops(rows)
        logger.info("Транспорт %s: остановок %d, маршрутов %d, связок %d (из поиска)",
                    city.slug, len(stops), len(routes), len(rows))

        self.crawl_route_platforms(city, routes)
        self.enrich_stops(city)
        save_csv(self.storage.transport_export(city.slug, "stops"), self.out_dir / f"dgis_stops_{city.slug}.csv")
        save_csv(self.storage.transport_export(city.slug, "routes"), self.out_dir / f"dgis_routes_{city.slug}.csv")
        save_csv(self.storage.transport_export(city.slug, "platforms"),
                 self.out_dir / f"dgis_route_platforms_{city.slug}.csv")

    def crawl_route_platforms(self, city: City, routes: Dict[str, Dict[str, Any]]) -> None:
        """
        Страница /route/{id}: остановки маршрута по порядку для каждого направления.
        Заодно дополняет остановки и связки маршрут × остановка тем, чего не было в поиске.
        Обходятся все маршруты города из БД без остановок — в том числе найденные прошлыми запусками.
        """
        todo = [
            r for r in self.storage.routes_without_platforms(city.slug)
            if (r["raw"] or {}).get("region_id") is None or self.in_city(city, [r["raw"]])
        ]
        logger.info("Маршруты %s: страниц к обходу %d (найдено в этом запуске %d)", city.slug, len(todo), len(routes))
        page = self.scraper.new_page()
        try:
            for idx, route in enumerate(todo, 1):
                page = self.scraper.recycle(page)
                html, _ = self.scraper.load_html(page, f"{SITE}/{city.slug}/route/{route['id']}")
                entity = state_entities(extract_initial_state(html or "")).get(route["id"])
                if not entity:
                    logger.warning("Маршрут %s (%s) не получен", route["id"], route.get("name"))
                    self.scraper.pace()
                    continue
                platforms, stops, links = [], {}, []
                for d_no, direction in enumerate(entity.get("directions") or []):
                    for seq, p in enumerate(direction.get("platforms") or [], 1):
                        m = re.match(r"POINT\(([\d.]+) ([\d.]+)\)", (p.get("geometry") or {}).get("centroid") or "")
                        lon, lat = (float(m.group(1)), float(m.group(2))) if m else (None, None)
                        stop_id = str(p.get("station_id") or p.get("id"))
                        platforms.append({
                            "city_slug": city.slug, "route_id": route["id"], "direction_no": d_no,
                            "direction_type": direction.get("type"), "seq": seq, "platform_id": p.get("id"),
                            "stop_id": stop_id, "stop_name": p.get("name"), "lat": lat, "lon": lon,
                        })
                        stops.setdefault(stop_id, {
                            "id": stop_id, "name": p.get("name") or "", "type": "station",
                            "subtype": entity.get("subtype") or route.get("subtype"), "lat": lat, "lon": lon,
                            "district": None, "region": city.region, "city": city.name, "city_slug": city.slug,
                            "raw": None,
                        })
                        links.append({
                            "region": city.region, "city": city.name, "city_slug": city.slug,
                            "route_id": route["id"], "route_number": entity.get("name") or route.get("name"),
                            "route_subtype": entity.get("subtype") or route.get("subtype") or "bus",
                            "route_from": entity.get("from_name"), "route_to": entity.get("to_name"),
                            "stop_id": stop_id, "stop_name": p.get("name"), "lat": lat, "lon": lon,
                            "district": None, "color": route.get("color"),
                        })
                self.storage.save_route_platforms(city.slug, route["id"], platforms)
                self.storage.mark_route_platforms_synced(city.slug, route["id"])
                self.storage.save_missing_transport_stops(list(stops.values()))
                self.storage.save_transport_route_stops(links)
                logger.info("[маршрут %d/%d] %s %s: направлений %d, остановок %d",
                            idx, len(todo), route.get("subtype"), route.get("name"),
                            len(entity.get("directions") or []), len(platforms))
                self.scraper.pace()
        except (CaptchaBlockedError, NetworkDownError) as e:
            logger.error("%s", e)
        finally:
            page.context.close()

    def enrich_stops(self, city: City) -> None:
        """Карточки остановок без адреса (встречены только на страницах маршрутов): город, район, микрорайон."""
        rows = self.storage.stops_without_address(city.slug)
        logger.info("Остановок без адреса: %d — открываем их карточки", len(rows))
        page = self.scraper.new_page()
        try:
            for idx, row in enumerate(rows, 1):
                page = self.scraper.recycle(page)
                html, _ = self.scraper.load_html(page, f"{SITE}/{city.slug}/geo/{row['id']}")
                entity = state_entities(extract_initial_state(html or "")).get(str(row["id"]))
                stop = stop_from_item(entity, city.slug, city.region, city.name) if entity else None
                if stop:
                    self.storage.save_transport_stops([stop])
                else:
                    logger.warning("Остановка %s (%s) не получена", row["id"], row["name"])
                if idx % 50 == 0:
                    logger.info("[остановки] %d/%d", idx, len(rows))
                self.scraper.pace()
        except (CaptchaBlockedError, NetworkDownError) as e:
            logger.error("%s", e)
        finally:
            page.context.close()

    # ------------------------------------------------------------------ отзывы

    def crawl_reviews(self, city: City, branches: List[Dict[str, Any]]) -> int:
        """Все отзывы объектов: вкладка отзывов + «Загрузить ещё» до конца."""
        total_saved = 0
        page = self.scraper.new_page()
        try:
            for idx, branch in enumerate(branches, 1):
                page = self.scraper.recycle(page)
                try:
                    reviews, comments, ended = self._fetch_reviews(page, city, int(branch["id"]))
                except (NetworkDownError, CaptchaBlockedError):
                    raise
                except Exception as e:
                    logger.error("Отзывы %s (%s): %s — объект остаётся в очереди", branch["id"], branch.get("name"), e)
                    self.storage.mark_reviews_incomplete(int(branch["id"]))
                    continue
                saved = self.storage.save_reviews(reviews)
                self.storage.save_review_comments(comments)
                # лента дошла до конца сама; число в карточке может включать отзывы, которых
                # в ленте нет, поэтому сверка с ним — только «0 собрано при непустой карточке»
                want = int(branch.get("review_count") or 0)
                complete = ended and not (saved == 0 and want > 0)
                if complete:
                    self.storage.mark_reviews_synced(int(branch["id"]))
                else:
                    self.storage.mark_reviews_incomplete(int(branch["id"]))
                total_saved += saved
                logger.info("[%d/%d] %s: отзывов %d (в карточке %s), комментариев %d%s",
                            idx, len(branches), branch.get("name"), saved, branch.get("review_count"),
                            len(comments), "" if complete else "  НЕПОЛНО — останется в очереди")
                self.scraper.pace()
        except (CaptchaBlockedError, NetworkDownError) as e:
            logger.error("%s", e)
        finally:
            page.context.close()
        return total_saved

    def _fetch_reviews(self, page: Page, city: City, branch_id: int
                       ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
        """Все отзывы объекта и комментарии к ним (ответы организации, реплики)."""
        found: Dict[str, Dict[str, Any]] = {}
        comments: Dict[str, Tuple[str, Dict[str, Any]]] = {}

        def on_response(resp: Response) -> None:
            path = urllib.parse.urlparse(resp.url).path
            if "reviews" not in resp.url or resp.status != 200:
                return
            try:
                data = resp.json()
            except Exception:
                return
            m = re.search(r"/reviews/([^/]+)/comments$", path)
            if m:
                for c in data.get("comments") or []:
                    if isinstance(c, dict) and c.get("id"):
                        comments[str(c["id"])] = (m.group(1), c)
                return
            for r in data.get("reviews") or []:
                if isinstance(r, dict) and r.get("id"):
                    found[str(r["id"])] = r

        page.on("response", on_response)
        try:
            html = self.scraper.load_page(page, f"{SITE}/{city.slug}/firm/{branch_id}/tab/reviews")
            if html is None:
                raise RuntimeError("страница отзывов не загрузилась")
            for r in embedded_reviews(html):
                found[str(r["id"])] = r
            # следующие страницы сайт запрашивает XHR по next_link при «Загрузить ещё»
            ended = self._load_more(page, lambda: len(found), None)
            if any(int(r.get("comments_count") or 0) for r in found.values()):
                page.wait_for_timeout(1500)  # комментарии к последним отзывам догружаются отдельно
        finally:
            page.remove_listener("response", on_response)
        return (
            [parse_review(r, branch_id) for r in found.values()],
            [parse_review_comment(c, rid, branch_id) for rid, c in comments.values() if rid in found],
            ended,
        )
