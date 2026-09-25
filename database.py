"""
Модуль асинхронной работы с базой данных SQLite для парсера 2ГИС.
Реализует хранение организаций и отзывов с поддержкой UPSERT и индексов.
"""

import json
import logging
from typing import Any, Dict, List, Optional
import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """Асинхронный клиент для взаимодействия с SQLite базой данных."""

    def __init__(self, db_path: str = "2gis_data.db"):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        """Устанавливает соединение с БД и настраивает PRAGMA для производительности и целостности."""
        if self._connection is None:
            self._connection = await aiosqlite.connect(self.db_path)
            self._connection.row_factory = aiosqlite.Row
            # Включаем WAL-режим для быстродействия при параллельном чтении/записи
            await self._connection.execute("PRAGMA journal_mode = WAL;")
            # Включаем проверку внешних ключей
            await self._connection.execute("PRAGMA foreign_keys = ON;")
            # Нормальная синхронизация для баланса надежности и скорости
            await self._connection.execute("PRAGMA synchronous = NORMAL;")
            logger.info("Подключение к SQLite БД успешно установлено: %s", self.db_path)

    async def close(self) -> None:
        """Корректно закрывает соединение с базой данных."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            logger.info("Соединение с БД закрыто.")

    async def __aenter__(self):
        await self.connect()
        await self.init_db()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def init_db(self) -> None:
        """Инициализирует структуру таблиц и необходимые индексы."""
        if self._connection is None:
            await self.connect()

        # Создаем таблицу организаций
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS places (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                legal_name TEXT,
                categories TEXT,
                latitude REAL,
                longitude REAL,
                address TEXT,
                floor TEXT,
                phones TEXT,
                websites TEXT,
                social_media TEXT,
                operating_hours TEXT,
                rating REAL,
                reviews_summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Создаем таблицу отзывов, связанную по внешнему ключу с places
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                place_id TEXT NOT NULL,
                author_name TEXT,
                rating REAL,
                text TEXT,
                date TEXT,
                likes INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (place_id) REFERENCES places(id) ON DELETE CASCADE
            );
        """)

        # Создаем индексы для ускорения поиска по координатам, внешнему ключу и дате
        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_places_coords 
            ON places(latitude, longitude);
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_place_id 
            ON comments(place_id);
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_date 
            ON comments(date);
        """)

        await self._connection.commit()
        logger.info("Схема БД успешно инициализирована.")

    async def save_place(self, place: Dict[str, Any]) -> None:
        """
        Сохраняет или обновляет организацию (UPSERT).
        Сериализует списки и словари в JSON-строки для хранения в SQLite.
        """
        if self._connection is None:
            await self.connect()

        # Сериализация сложных полей в JSON
        categories_json = json.dumps(place.get("categories", []), ensure_ascii=False)
        phones_json = json.dumps(place.get("phones", []), ensure_ascii=False)
        websites_json = json.dumps(place.get("websites", []), ensure_ascii=False)
        social_media_json = json.dumps(place.get("social_media", {}), ensure_ascii=False)
        operating_hours_json = json.dumps(place.get("operating_hours", {}), ensure_ascii=False)
        reviews_summary_json = json.dumps(place.get("reviews_summary", {}), ensure_ascii=False)

        query = """
            INSERT INTO places (
                id, name, legal_name, categories, latitude, longitude,
                address, floor, phones, websites, social_media,
                operating_hours, rating, reviews_summary, updated_at
            ) VALUES (
                :id, :name, :legal_name, :categories, :latitude, :longitude,
                :address, :floor, :phones, :websites, :social_media,
                :operating_hours, :rating, :reviews_summary, CURRENT_TIMESTAMP
            )
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                legal_name = excluded.legal_name,
                categories = excluded.categories,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                address = excluded.address,
                floor = excluded.floor,
                phones = excluded.phones,
                websites = excluded.websites,
                social_media = excluded.social_media,
                operating_hours = excluded.operating_hours,
                rating = excluded.rating,
                reviews_summary = excluded.reviews_summary,
                updated_at = CURRENT_TIMESTAMP;
        """

        params = {
            "id": str(place["id"]),
            "name": place.get("name", ""),
            "legal_name": place.get("legal_name"),
            "categories": categories_json,
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "address": place.get("address"),
            "floor": place.get("floor"),
            "phones": phones_json,
            "websites": websites_json,
            "social_media": social_media_json,
            "operating_hours": operating_hours_json,
            "rating": place.get("rating"),
            "reviews_summary": reviews_summary_json,
        }

        await self._connection.execute(query, params)
        await self._connection.commit()

    async def save_comments(self, comments: List[Dict[str, Any]]) -> int:
        """
        Массовое сохранение или обновление отзывов (bulk UPSERT).
        Возвращает количество обработанных записей.
        """
        if not comments:
            return 0

        if self._connection is None:
            await self.connect()

        query = """
            INSERT INTO comments (
                id, place_id, author_name, rating, text, date, likes
            ) VALUES (
                :id, :place_id, :author_name, :rating, :text, :date, :likes
            )
            ON CONFLICT(id) DO UPDATE SET
                author_name = excluded.author_name,
                rating = excluded.rating,
                text = excluded.text,
                date = excluded.date,
                likes = excluded.likes;
        """

        prepared_records = []
        for c in comments:
            prepared_records.append({
                "id": str(c["id"]),
                "place_id": str(c["place_id"]),
                "author_name": c.get("author_name"),
                "rating": c.get("rating"),
                "text": c.get("text"),
                "date": str(c.get("date")) if c.get("date") else None,
                "likes": int(c.get("likes") or 0),
            })

        await self._connection.executemany(query, prepared_records)
        await self._connection.commit()
        return len(prepared_records)

    async def get_stats(self) -> Dict[str, int]:
        """Возвращает количество записей в таблицах places и comments."""
        if self._connection is None:
            await self.connect()

        async with self._connection.execute("SELECT COUNT(*) FROM places;") as cursor:
            places_count = (await cursor.fetchone())[0]

        async with self._connection.execute("SELECT COUNT(*) FROM comments;") as cursor:
            comments_count = (await cursor.fetchone())[0]

        return {"places": places_count, "comments": comments_count}
