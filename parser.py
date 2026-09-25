"""
Модуль сбора и парсинга данных 2ГИС: регионы, рубрикатор, филиалы и отзывы.

Полный обход региона:
1. Регион ищется по названию (или ID), берутся его границы (bbox).
2. Загружается рубрикатор региона; для каждой конечной рубрики (или поискового
   запроса) в очередь crawl_tasks кладётся тайл размером с весь регион.
3. Воркер берёт тайл и ищет филиалы внутри полигона тайла. Если выдача больше,
   чем API позволяет пролистать (page_size * max_pages), тайл делится на 4 части.
4. Для каждого собранного филиала выгружаются все отзывы с пагинацией.
Очередь хранится в БД, поэтому прерванный обход продолжается с места остановки.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from client import AntiBanHttpClient, ApiKeyBlockedError
from storage import Storage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from web_fallback import BrowserFallback

logger = logging.getLogger(__name__)

# Дефолтные публичные ключи (могут быть переопределены через аргументы/env)
DEFAULT_CATALOG_KEY = "rutnpt3272"
DEFAULT_REVIEWS_KEY = "rurbbn3446"

CATALOG_API = "https://catalog.api.2gis.com"
ITEMS_URL = f"{CATALOG_API}/3.0/items"
REGION_SEARCH_URL = f"{CATALOG_API}/2.0/region/search"
REGION_GET_URL = f"{CATALOG_API}/2.0/region/get"
RUBRIC_LIST_URL = f"{CATALOG_API}/2.0/catalog/rubric/list"
REGION_LIST_URL = f"{CATALOG_API}/2.0/region/list"
REVIEWS_API_URL = "https://public-api.reviews.2gis.com/2.0/branches"

# Slug города для URL вида https://2gis.kz/<slug>/... (браузерный фолбэк).
# Ключ — название региона/города, как его отдаёт Catalog API region/search
# и region/list (см. TwoGisCrawler.resolve_region / list_country_regions).
# Список проверен пользователем в dgis_stations_parser.py (REGIONS).
REGION_SLUGS: Dict[str, str] = {
    "Астана": "astana",
    "Алматы": "almaty",
    "Шымкент": "shymkent",
    "Актау": "aktau",
    "Актобе": "aktobe",
    "Атырау": "atyrau",
    "Жезказган": "zhezkazgan",
    "Караганда": "karaganda",
    "Кокшетау": "kokshetau",
    "Костанай": "kostanay",
    "Кызылорда": "kyzylorda",
    "Павлодар": "pavlodar",
    "Петропавловск": "petropavlovsk",
    "Семей": "semey",
    "Тараз": "taraz",
    "Туркестан": "turkestan",
    "Уральск": "uralsk",
    "Усть-Каменогорск": "ust-kamenogorsk",
    "Экибастуз": "ekibastuz",
    "Талдыкорган": "taldykorgan",
}

# Все поля карточки филиала, которые отдаёт Catalog API 3.0
ITEM_FIELDS = ",".join(
    f"items.{f}"
    for f in (
        "point", "address", "adm_div", "full_address_name", "name_ex", "org",
        "rubrics", "contact_groups", "schedule", "timezone", "reviews",
        "attribute_groups", "flags", "links", "external_content", "dates",
        "floors", "region_id", "description",
    )
)

REVIEW_FIELDS = ",".join(
    (
        "meta.providers", "meta.branch_rating", "meta.branch_reviews_count",
        "meta.total_count", "reviews.hiding_reason", "reviews.is_verified",
    )
)

Bbox = Tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


class ApiError(RuntimeError):
    """Ошибка, которую 2ГИС вернул в meta ответа."""

    def __init__(self, code: int, message: str):
        super().__init__(f"2GIS API {code}: {message}")
        self.code = code


def _check_meta(data: Dict[str, Any]) -> bool:
    """Проверяет meta ответа Catalog API. False — результатов нет (код 404)."""
    meta = data.get("meta") or {}
    code = meta.get("code", 200)
    if code == 200:
        return True
    if code == 404:
        return False
    error = meta.get("error") or {}
    raise ApiError(code, error.get("message") or error.get("type") or "unknown error")


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).split("_")[0])
    except (TypeError, ValueError):
        return None


def parse_wkt_bbox(wkt: Optional[str]) -> Optional[Bbox]:
    """Возвращает bbox для WKT-геометрии (POLYGON / MULTIPOLYGON)."""
    if not wkt:
        return None
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", wkt)]
    if len(numbers) < 4:
        return None
    lons, lats = numbers[0::2], numbers[1::2]
    return min(lons), min(lats), max(lons), max(lats)


def bbox_polygon(bbox: Bbox) -> str:
    x1, y1, x2, y2 = bbox
    return f"POLYGON(({x1} {y1},{x2} {y1},{x2} {y2},{x1} {y2},{x1} {y1}))"


def split_bbox(bbox: Bbox) -> List[Bbox]:
    x1, y1, x2, y2 = bbox
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    return [(x1, y1, mx, my), (mx, y1, x2, my), (x1, my, mx, y2), (mx, my, x2, y2)]


def parse_region(item: Dict[str, Any]) -> Dict[str, Any]:
    bbox = parse_wkt_bbox(item.get("bounds"))
    min_lon, min_lat, max_lon, max_lat = bbox or (None, None, None, None)
    return {
        "id": int(item["id"]),
        "name": item.get("name") or "",
        "type": item.get("type"),
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "raw": item,
    }


def parse_rubric(item: Dict[str, Any], parent_id: Optional[int]) -> Dict[str, Any]:
    return {
        "id": int(item["id"]),
        "parent_id": _to_int(item.get("parent_id")) or parent_id,
        "name": item.get("name") or "",
        "alias": item.get("alias"),
        "type": item.get("type"),
        "raw": {k: v for k, v in item.items() if k != "rubrics"},
    }


def parse_branch(item: Dict[str, Any], region_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Преобразует карточку филиала из Catalog API 3.0 в структуру для БД."""
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
                "value": value,
                "text": c.get("print_text") or c.get("text"),
                "url": c.get("url"),
                "comment": c.get("comment"),
                "position": len(contacts),
            })

    return {
        "id": branch_id,
        "raw_id": raw_id,
        "org_id": org_id,
        "org": org,
        "region_id": _to_int(item.get("region_id")) or region_id,
        "name": item.get("name") or name_ex.get("primary") or "",
        "name_primary": name_ex.get("primary"),
        "name_extension": name_ex.get("extension"),
        "legal_name": name_ex.get("legal_name"),
        "address_name": item.get("address_name"),
        "full_address_name": item.get("full_address_name"),
        "address_comment": item.get("address_comment"),
        "postcode": address.get("postcode"),
        "building_id": _to_int(address.get("building_id")),
        "city": adm_div.get("city") or adm_div.get("settlement"),
        "district": adm_div.get("district"),
        "lat": point.get("lat"),
        "lon": point.get("lon"),
        "rating": reviews.get("general_rating") or reviews.get("rating"),
        "review_count": reviews.get("general_review_count") or reviews.get("review_count"),
        "org_rating": reviews.get("org_rating"),
        "org_review_count": reviews.get("org_review_count"),
        "schedule": item.get("schedule"),
        "timezone": item.get("timezone"),
        "attributes": item.get("attribute_groups"),
        "flags": item.get("flags"),
        "rubrics": rubrics,
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


class TwoGisCrawler:
    """Полный обход каталога 2ГИС с сохранением в PostgreSQL."""

    def __init__(
        self,
        client: AntiBanHttpClient,
        storage: Storage,
        catalog_key: str = DEFAULT_CATALOG_KEY,
        reviews_key: str = DEFAULT_REVIEWS_KEY,
        page_size: int = 10,
        max_pages: int = 5,
        min_tile_deg: float = 0.002,
        max_task_attempts: int = 3,
    ):
        self.client = client
        self.storage = storage
        self.catalog_key = catalog_key
        self.reviews_key = reviews_key
        self.page_size = page_size
        self.max_pages = max_pages
        self.min_tile_deg = min_tile_deg
        self.max_task_attempts = max_task_attempts

    @property
    def result_cap(self) -> int:
        """Сколько результатов можно пролистать по одному запросу."""
        return self.page_size * self.max_pages

    # ------------------------------------------------------------------ регион и рубрики

    async def resolve_region(self, name: Optional[str] = None,
                             region_id: Optional[int] = None) -> Dict[str, Any]:
        """Находит регион по названию или ID и сохраняет его в БД."""
        if region_id is not None:
            params = {"id": region_id, "fields": "items.bounds", "key": self.catalog_key}
            data = await self.client.request_json(REGION_GET_URL, params=params)
        else:
            params = {"q": name, "fields": "items.bounds", "key": self.catalog_key}
            data = await self.client.request_json(REGION_SEARCH_URL, params=params)

        if not _check_meta(data):
            raise ApiError(404, f"Регион не найден: {name or region_id}")
        items = (data.get("result") or {}).get("items") or []
        if not items:
            raise ApiError(404, f"Регион не найден: {name or region_id}")

        region = parse_region(items[0])
        await self.storage.save_region(region)
        logger.info("Регион: %s (ID %s), bbox: %s", region["name"], region["id"],
                    (region["min_lon"], region["min_lat"], region["max_lon"], region["max_lat"]))
        return region

    async def list_country_regions(self, country_code: str = "kz") -> List[Dict[str, Any]]:
        """Возвращает и сохраняет список всех регионов страны (например, 'kz')."""
        params = {
            "country_code_filter": country_code,
            "page_size": 50,
            "fields": "items.bounds",
            "key": self.catalog_key,
        }
        data = await self.client.request_json(REGION_LIST_URL, params=params)
        if not _check_meta(data):
            return []
        items = (data.get("result") or {}).get("items") or []
        regions = []
        for item in items:
            reg = parse_region(item)
            await self.storage.save_region(reg)
            regions.append(reg)
        logger.info("Найдено регионов для страны '%s': %d", country_code, len(regions))
        return regions

    async def load_rubrics(self, region_id: int) -> List[Dict[str, Any]]:
        """Рекурсивно загружает рубрикатор региона. Возвращает все рубрики (группы и конечные)."""
        collected: Dict[int, Dict[str, Any]] = {}

        async def walk(parent_id: int) -> None:
            params = {"region_id": region_id, "parent_id": parent_id, "key": self.catalog_key}
            data = await self.client.request_json(RUBRIC_LIST_URL, params=params)
            if not _check_meta(data):
                return
            for item in (data.get("result") or {}).get("items") or []:
                rubric = parse_rubric(item, parent_id or None)
                if rubric["id"] in collected:
                    continue
                collected[rubric["id"]] = rubric
                if rubric["type"] == "group":
                    await walk(rubric["id"])

        await walk(0)
        rubrics = list(collected.values())
        await self.storage.save_rubrics(rubrics)
        logger.info("Загружено рубрик: %d (конечных: %d)", len(rubrics),
                    sum(1 for r in rubrics if r["type"] != "group"))
        return rubrics

    async def seed_tasks(
        self,
        region: Dict[str, Any],
        bbox: Bbox,
        queries: Optional[List[str]] = None,
        rubric_filter: Optional[List[str]] = None,
    ) -> None:
        """Кладёт в очередь стартовые тайлы: по одному на рубрику или поисковый запрос."""
        base = {
            "region_id": region["id"],
            "min_lon": bbox[0], "min_lat": bbox[1], "max_lon": bbox[2], "max_lat": bbox[3],
        }
        if queries:
            tasks = [{**base, "query": q} for q in queries]
        else:
            rubrics = await self.load_rubrics(region["id"])
            leaves = [
                r for r in rubrics
                if r["type"] != "group" and r["raw"].get("branch_count") != 0
            ]
            if rubric_filter:
                needles = [f.lower() for f in rubric_filter]
                leaves = [r for r in leaves if any(n in r["name"].lower() for n in needles)]
            tasks = [{**base, "rubric_id": r["id"]} for r in leaves]
        await self.storage.add_tasks(tasks)
        logger.info("В очередь добавлено стартовых задач: %d", len(tasks))

    # ------------------------------------------------------------------ каталог

    async def _search_page(self, task: Dict[str, Any], bbox: Bbox, page: int
                           ) -> Tuple[int, List[Dict[str, Any]]]:
        params: Dict[str, Any] = {
            "key": self.catalog_key,
            "type": "branch",
            "polygon": bbox_polygon(bbox),
            "page": page,
            "page_size": self.page_size,
            "fields": ITEM_FIELDS,
        }
        if task.get("rubric_id"):
            params["rubric_id"] = task["rubric_id"]
        if task.get("query"):
            params["q"] = task["query"]

        data = await self.client.request_json(ITEMS_URL, params=params)
        if not _check_meta(data):
            return 0, []
        result = data.get("result") or {}
        return int(result.get("total") or 0), result.get("items") or []

    async def process_task(self, task: Dict[str, Any]) -> None:
        bbox: Bbox = (task["min_lon"], task["min_lat"], task["max_lon"], task["max_lat"])
        label = f"задача #{task['id']} (рубрика={task['rubric_id']}, q={task['query']}, " \
                f"глубина={task['depth']})"

        total, items = await self._search_page(task, bbox, 1)
        saved = await self._save_items(items, task["region_id"])

        can_split = (bbox[2] - bbox[0]) > self.min_tile_deg and (bbox[3] - bbox[1]) > self.min_tile_deg
        if total > self.result_cap and can_split:
            children = [
                {
                    "region_id": task["region_id"], "rubric_id": task["rubric_id"],
                    "query": task["query"], "depth": task["depth"] + 1,
                    "min_lon": b[0], "min_lat": b[1], "max_lon": b[2], "max_lat": b[3],
                }
                for b in split_bbox(bbox)
            ]
            await self.storage.add_tasks(children)
            await self.storage.finish_task(task["id"], "split", total, saved)
            logger.info("%s: найдено %d > %d, тайл разбит на 4.", label, total, self.result_cap)
            return

        if total > self.result_cap:
            logger.warning("%s: %d результатов в минимальном тайле, будет собрано не более %d.",
                           label, total, self.result_cap)

        page = 2
        while saved < min(total, self.result_cap) and page <= self.max_pages:
            try:
                _, items = await self._search_page(task, bbox, page)
            except ApiError as exc:
                logger.warning("%s: страница %d недоступна (%s), останавливаем листание.",
                               label, page, exc)
                break
            if not items:
                break
            saved += await self._save_items(items, task["region_id"])
            page += 1

        await self.storage.finish_task(task["id"], "done", total, saved)
        logger.info("%s: собрано %d из %d.", label, saved, total)

    async def _save_items(self, items: List[Dict[str, Any]], region_id: int) -> int:
        branches = [b for b in (parse_branch(i, region_id) for i in items) if b]
        return await self.storage.save_branches(branches)

    async def crawl_catalog(self, region_id: int, workers: int = 1) -> None:
        """Обрабатывает очередь тайлов, пока она не опустеет."""
        reset = await self.storage.reset_running_tasks()
        if reset:
            logger.info("Возвращено в очередь незавершённых задач: %d", reset)

        async def worker(n: int) -> None:
            idle_rounds = 0
            while True:
                task = await self.storage.claim_task(region_id)
                if task is None:
                    # Другой воркер мог ещё добавить дочерние тайлы
                    stats = await self.storage.task_stats(region_id)
                    if not stats.get("running") or idle_rounds > 60:
                        return
                    idle_rounds += 1
                    await asyncio.sleep(1)
                    continue
                idle_rounds = 0
                try:
                    await self.process_task(task)
                except asyncio.CancelledError:
                    raise
                except ApiKeyBlockedError:
                    # Ключ заблокирован окончательно — ретраить бессмысленно,
                    # останавливаем весь обход, чтобы вызывающий код мог
                    # переключиться на браузерный фолбэк (см. main.py).
                    raise
                except Exception as exc:
                    status = "error" if task["attempts"] >= self.max_task_attempts else "pending"
                    await self.storage.finish_task(task["id"], status, task["total"],
                                                   task["fetched"], str(exc)[:1000])
                    logger.error("Воркер %d: задача #%d завершилась ошибкой (%s): %s",
                                 n, task["id"], status, exc)

        await asyncio.gather(*(worker(i + 1) for i in range(workers)))
        stats = await self.storage.task_stats(region_id)
        logger.info("Обход каталога завершён. Статусы задач: %s", stats)

    # ------------------------------------------------------------------ отзывы

    def _with_reviews_key(self, url: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query.setdefault("key", self.reviews_key)
        return urlunsplit(parts._replace(query=urlencode(query)))

    async def fetch_reviews(self, branch_id: int, incremental: bool) -> int:
        """
        Выгружает все отзывы филиала постранично (по meta.next_link).
        В инкрементальном режиме останавливается на первой странице без новых отзывов.
        """
        url: Optional[str] = f"{REVIEWS_API_URL}/{branch_id}/reviews"
        params: Optional[Dict[str, Any]] = {
            "key": self.reviews_key,
            "limit": 50,
            "sort_by": "date_created",
            "fields": REVIEW_FIELDS,
            "without_my_first_review": "false",
            "locale": "ru_RU",
        }
        saved = 0
        tried_fallback = False
        while url:
            try:
                data = await self.client.request_json(url, params=params)
            except ApiKeyBlockedError as exc:
                if not tried_fallback and self.reviews_key != DEFAULT_REVIEWS_KEY:
                    logger.warning(
                        "Ключ отзывов %s заблокирован (%s). Переключаемся на резервный ключ %s",
                        self.reviews_key, exc, DEFAULT_REVIEWS_KEY
                    )
                    self.reviews_key = DEFAULT_REVIEWS_KEY
                    tried_fallback = True
                    if params:
                        params["key"] = DEFAULT_REVIEWS_KEY
                    if url:
                        url = self._with_reviews_key(url)
                    continue
                # Оба ключа заблокированы — пробрасываем наружу, чтобы
                # crawl_reviews() мог переключиться на браузерный фолбэк.
                raise
            except Exception as exc:
                logger.error("Ошибка при получении отзывов филиала %s: %s", branch_id, exc)
                break
            params = None
            raw_reviews = data.get("reviews") or []
            if not raw_reviews:
                break
            reviews = [parse_review(r, branch_id) for r in raw_reviews if r.get("id")]
            known = await self.storage.known_review_ids([r["id"] for r in reviews]) \
                if incremental else set()
            saved += await self.storage.save_reviews(reviews)
            if incremental and len(known) == len(reviews):
                break
            next_link = (data.get("meta") or {}).get("next_link")
            url = self._with_reviews_key(next_link) if next_link else None
        await self.storage.mark_reviews_synced(branch_id)
        return saved

    async def crawl_reviews(self, region_id: Optional[int], workers: int = 1,
                            only_missing: bool = False) -> int:
        branches = await self.storage.branches_for_reviews(region_id, only_missing)
        logger.info("Сбор отзывов: филиалов в очереди %d", len(branches))
        queue: asyncio.Queue = asyncio.Queue()
        for b in branches:
            queue.put_nowait(b)
        total_saved = 0
        processed = 0

        async def worker() -> None:
            nonlocal total_saved, processed
            while not queue.empty():
                branch = queue.get_nowait()
                try:
                    saved = await self.fetch_reviews(
                        branch["id"], incremental=branch["reviews_synced_at"] is not None
                    )
                    total_saved += saved
                except asyncio.CancelledError:
                    raise
                except ApiKeyBlockedError:
                    raise
                except Exception as exc:
                    logger.error("Отзывы филиала %s: %s", branch["id"], exc)
                    continue
                processed += 1
                if saved:
                    logger.info("[%d/%d] %s: сохранено отзывов %d", processed, len(branches),
                                branch["name"], saved)

        await asyncio.gather(*(worker() for _ in range(workers)))
        logger.info("Сбор отзывов завершён: филиалов %d, отзывов сохранено/обновлено %d",
                    processed, total_saved)
        return total_saved

    # ------------------------------------------------------------------ браузерный фолбэк

    @staticmethod
    def region_slug(region_name: str) -> Optional[str]:
        """Slug города 2ГИС для URL https://2gis.kz/<slug>/... по имени региона."""
        return REGION_SLUGS.get(region_name)

    async def crawl_catalog_via_browser(
        self,
        fallback: "BrowserFallback",
        region_id: int,
        city_slug: str,
        queries: List[str],
        max_pages: int = 50,
    ) -> int:
        """
        Обход каталога через реальный браузер (когда API-ключ заблокирован):
        постранично открывает поиск на сайте 2ГИС и сохраняет перехваченные
        карточки организаций. Полноту не гарантирует (сайт отдаёт то же
        ограничение глубины пагинации, что и в обычном UI), но не требует
        рабочего API-ключа вообще.
        """
        total_saved = 0
        for query in queries:
            logger.info("Браузерный обход: запрос '%s' в %s", query, city_slug)
            empty_streak = 0
            for page_num in range(1, max_pages + 1):
                try:
                    items = await fallback.search_organizations(query, city_slug, page_num)
                except Exception as exc:
                    logger.error("Браузерный фолбэк: ошибка страницы %d ('%s'): %s",
                                 page_num, query, exc)
                    break
                if not items:
                    empty_streak += 1
                    if empty_streak >= 2:
                        break
                    continue
                empty_streak = 0
                saved = await self._save_items(items, region_id)
                total_saved += saved
                logger.info("Браузерный обход: '%s' стр. %d — сохранено %d карточек",
                            query, page_num, saved)
        logger.info("Браузерный обход каталога завершён. Всего сохранено: %d", total_saved)
        return total_saved

    async def crawl_reviews_via_browser(
        self,
        fallback: "BrowserFallback",
        region_id: Optional[int],
        city_slug: str,
        only_missing: bool = False,
    ) -> int:
        """Собирает отзывы через браузер для всех сохранённых филиалов региона."""
        branches = await self.storage.branches_for_reviews(region_id, only_missing)
        logger.info("Браузерный сбор отзывов: филиалов в очереди %d", len(branches))
        total_saved = 0
        for i, branch in enumerate(branches, 1):
            try:
                raw_reviews = await fallback.fetch_reviews(branch["id"], city_slug)
            except Exception as exc:
                logger.error("Браузерный фолбэк: отзывы филиала %s: %s", branch["id"], exc)
                continue
            if not raw_reviews:
                continue
            reviews = [parse_review(r, branch["id"]) for r in raw_reviews if r.get("id")]
            saved = await self.storage.save_reviews(reviews)
            await self.storage.mark_reviews_synced(branch["id"])
            total_saved += saved
            logger.info("[%d/%d] %s: сохранено отзывов %d (браузер)",
                        i, len(branches), branch["name"], saved)
        logger.info("Браузерный сбор отзывов завершён: сохранено %d", total_saved)
        return total_saved
