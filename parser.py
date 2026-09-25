"""
Модуль сбора и парсинга данных каталога 2ГИС и отзывов пользователей.
Извлекает полную информацию: координаты, категории, контакты, время работы,
а также выполняет пагинацию отзывов для каждой найденной организации.
"""

import logging
from typing import Any, Dict, List, Optional
from client import AntiBanHttpClient
from database import Database

logger = logging.getLogger(__name__)

# Дефолтные публичные ключи (могут быть переопределены через аргументы/env)
DEFAULT_CATALOG_KEY = "rutnpt3272"
DEFAULT_REVIEWS_KEY = "rurbbn3446"

CATALOG_API_URL = "https://catalog.api.2gis.com/3.0/items"
REVIEWS_API_URL = "https://public-api.reviews.2gis.com/2.0/branches"


class TwoGisParser:
    """Парсер данных 2ГИС: каталог организаций и отзывы с пагинацией."""

    def __init__(
        self,
        client: AntiBanHttpClient,
        db: Database,
        catalog_key: str = DEFAULT_CATALOG_KEY,
        reviews_key: str = DEFAULT_REVIEWS_KEY,
    ):
        self.client = client
        self.db = db
        self.catalog_key = catalog_key
        self.reviews_key = reviews_key

    @staticmethod
    def extract_branch_id(full_id: str) -> str:
        """Извлекает чистый ID филиала (отсекая хэш-суффикс, например 70000001034739765_...)."""
        if not full_id:
            return ""
        return str(full_id).split("_")[0]

    def parse_place_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Преобразует сырой JSON-объект организации из 2ГИС в унифицированную структуру.
        Обязательно извлекает координаты, адрес, этаж, категории и контакты.
        """
        raw_id = str(item.get("id", ""))
        branch_id = self.extract_branch_id(raw_id)

        # Координаты (широта и долгота)
        point = item.get("point") or {}
        lat = point.get("lat")
        lon = point.get("lon")

        # Название и юр. лицо
        name = item.get("name", "")
        org_data = item.get("org") or {}
        legal_name = org_data.get("name") or org_data.get("primary") or name

        # Категории (рубрики)
        categories = [
            rub.get("name")
            for rub in item.get("rubrics", [])
            if isinstance(rub, dict) and rub.get("name")
        ]

        # Адрес и этаж
        address = item.get("address_name")
        if not address and isinstance(item.get("address"), dict):
            address = item.get("address", {}).get("building_name")
        floor = item.get("address_comment")

        # Контакты (телефоны, сайты, соцсети)
        phones: List[str] = []
        websites: List[str] = []
        social_media: Dict[str, List[str]] = {}

        social_types = {
            "vkontakte", "telegram", "whatsapp", "instagram",
            "facebook", "youtube", "twitter", "viber", "ok", "tiktok"
        }

        for group in item.get("contact_groups", []):
            if not isinstance(group, dict):
                continue
            for contact in group.get("contacts", []):
                if not isinstance(contact, dict):
                    continue
                c_type = contact.get("type", "").lower()
                c_val = contact.get("value") or contact.get("text") or contact.get("url")
                if not c_val:
                    continue

                if c_type == "phone":
                    # Используем печатный номер или значение
                    phone_val = contact.get("print_text") or contact.get("text") or c_val
                    if phone_val not in phones:
                        phones.append(phone_val)
                elif c_type == "website":
                    # Используем отображаемый URL или ссылку
                    site_url = contact.get("text") or contact.get("url") or c_val
                    if site_url not in websites:
                        websites.append(site_url)
                elif c_type in social_types:
                    if c_type not in social_media:
                        social_media[c_type] = []
                    if c_val not in social_media[c_type]:
                        social_media[c_type].append(c_val)

        # Время работы
        operating_hours = item.get("schedule") or {}

        # Рейтинг и сводка отзывов
        reviews_info = item.get("reviews") or {}
        rating = reviews_info.get("general_rating") or reviews_info.get("rating")
        reviews_summary = {
            "rating": rating,
            "review_count": reviews_info.get("general_review_count") or reviews_info.get("review_count"),
            "review_count_with_stars": reviews_info.get("general_review_count_with_stars"),
            "org_rating": reviews_info.get("org_rating"),
            "org_review_count": reviews_info.get("org_review_count"),
        }

        return {
            "id": branch_id,
            "raw_id": raw_id,
            "name": name,
            "legal_name": legal_name,
            "categories": categories,
            "latitude": lat,
            "longitude": lon,
            "address": address,
            "floor": floor,
            "phones": phones,
            "websites": websites,
            "social_media": social_media,
            "operating_hours": operating_hours,
            "rating": rating,
            "reviews_summary": reviews_summary,
        }

    def parse_review_item(self, review: Dict[str, Any], place_id: str) -> Dict[str, Any]:
        """Преобразует сырой отзыв 2ГИС в схему БД."""
        user = review.get("user") or {}
        author_name = user.get("name") or user.get("first_name") or "Пользователь 2ГИС"

        return {
            "id": str(review.get("id")),
            "place_id": str(place_id),
            "author_name": author_name,
            "rating": review.get("rating"),
            "text": review.get("text") or "",
            "date": review.get("date_created"),
            "likes": int(review.get("likes_count") or 0),
        }

    async def fetch_reviews_for_place(
        self,
        branch_id: str,
        max_reviews: int = 100,
        page_limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Собирает отзывы для организации с учетом пагинации (по next_link / offset_date).
        """
        all_reviews: List[Dict[str, Any]] = []
        next_url: Optional[str] = f"{REVIEWS_API_URL}/{branch_id}/reviews"
        params: Optional[Dict[str, Any]] = {
            "limit": page_limit,
            "key": self.reviews_key,
            "sort_by": "date_created",
        }

        logger.info("Начало сбора отзывов для филиала %s (лимит: %d)", branch_id, max_reviews)

        while next_url and len(all_reviews) < max_reviews:
            try:
                data = await self.client.request_json(next_url, params=params)
                # После первого запроса параметры пагинации уже содержатся в next_link
                params = None

                raw_reviews = data.get("reviews") or []
                if not raw_reviews:
                    break

                for r in raw_reviews:
                    parsed = self.parse_review_item(r, branch_id)
                    all_reviews.append(parsed)
                    if len(all_reviews) >= max_reviews:
                        break

                meta = data.get("meta") or {}
                next_url = meta.get("next_link")
                logger.debug(
                    "Филиал %s: загружено %d отзывов. След. страница: %s",
                    branch_id,
                    len(all_reviews),
                    bool(next_url),
                )

            except Exception as exc:
                logger.error("Ошибка при получении отзывов для филиала %s: %s", branch_id, exc)
                break

        return all_reviews

    async def search_and_parse(
        self,
        query: str,
        location: Optional[str] = None,
        city_id: Optional[str] = None,
        max_places: int = 50,
        page_size: int = 10,
        fetch_comments: bool = True,
        max_reviews_per_place: int = 50,
    ) -> Dict[str, int]:
        """
        Основной цикл парсинга:
        1. Постраничный поиск объектов в каталоге 2ГИС.
        2. Извлечение полных реквизитов, координат, контактов.
        3. Сохранение организации в локальную БД.
        4. Если включено: постраничный сбор отзывов и сохранение в связанную таблицу comments.
        """
        page = 1
        total_places_saved = 0
        total_comments_saved = 0

        fields = (
            "items.point,items.address,items.contact_groups,"
            "items.schedule,items.flags,items.adm_div,items.rubrics,"
            "items.reviews,items.org"
        )

        logger.info(
            "Старт парсинга по запросу '%s' (location=%s, city_id=%s, max_places=%d)",
            query,
            location,
            city_id,
            max_places,
        )

        while total_places_saved < max_places:
            params = {
                "q": query,
                "key": self.catalog_key,
                "page": page,
                "page_size": min(page_size, max_places - total_places_saved),
                "fields": fields,
            }
            if location:
                params["location"] = location
            if city_id:
                params["city_id"] = city_id

            try:
                data = await self.client.request_json(CATALOG_API_URL, params=params)
            except Exception as exc:
                logger.error("Критическая ошибка при запросе каталога: %s", exc)
                break

            result = data.get("result") or {}
            items = result.get("items") or []

            if not items:
                logger.info("Объекты в каталоге 2ГИС больше не найдены (страница %d).", page)
                break

            for raw_item in items:
                # Фильтруем только объекты типа branch (филиалы)
                if raw_item.get("type") and raw_item.get("type") != "branch":
                    continue

                place = self.parse_place_item(raw_item)
                await self.db.save_place(place)
                total_places_saved += 1

                logger.info(
                    "[%d/%d] Сохранена организация: %s (ID: %s, Коорд: %s, %s)",
                    total_places_saved,
                    max_places,
                    place["name"],
                    place["id"],
                    place["latitude"],
                    place["longitude"],
                )

                # Сбор связанных отзывов с пагинацией
                if fetch_comments and place["id"]:
                    reviews = await self.fetch_reviews_for_place(
                        branch_id=place["id"],
                        max_reviews=max_reviews_per_place,
                    )
                    if reviews:
                        saved_reviews = await self.db.save_comments(reviews)
                        total_comments_saved += saved_reviews
                        logger.info(
                            "   -> Сохранено %d отзывов для филиала '%s'",
                            saved_reviews,
                            place["name"],
                        )

                if total_places_saved >= max_places:
                    break

            page += 1

        logger.info(
            "Парсинг успешно завершен! Сохранено организаций: %d, отзывов: %d",
            total_places_saved,
            total_comments_saved,
        )
        return {"places": total_places_saved, "comments": total_comments_saved}
