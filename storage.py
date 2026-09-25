"""
Хранилище данных 2ГИС в PostgreSQL (отдельная схема, по умолчанию `twogis`).
Реализует UPSERT регионов, рубрик, организаций, филиалов, контактов и отзывов,
а также очередь тайлов обхода (crawl_tasks) для возобновляемого сбора.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "sql" / "schema.sql"


class Storage:
    """Асинхронный клиент PostgreSQL. Все операции сериализуются через один lock."""

    def __init__(self, dsn: str, schema: str = "twogis"):
        self.dsn = dsn
        self.schema = schema
        self._conn: Optional[psycopg.AsyncConnection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._conn is None:
            self._conn = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, row_factory=dict_row
            )
            await self._conn.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
            )
            logger.info("Подключение к PostgreSQL установлено (схема: %s).", self.schema)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Соединение с БД закрыто.")

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def init_db(self) -> None:
        """Создаёт схему и все таблицы/индексы/представления (идемпотентно)."""
        ddl = SCHEMA_FILE.read_text(encoding="utf-8")
        async with self._lock, self._conn.transaction():
            await self._conn.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
            )
            await self._conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema))
            )
            await self._conn.execute(ddl)
        logger.info("Схема '%s' инициализирована.", self.schema)

    # ------------------------------------------------------------------ справочники

    async def save_region(self, region: Dict[str, Any]) -> None:
        async with self._lock:
            await self._conn.execute(
                """
                INSERT INTO regions (id, name, type, min_lon, min_lat, max_lon, max_lat, raw)
                VALUES (%(id)s, %(name)s, %(type)s, %(min_lon)s, %(min_lat)s,
                        %(max_lon)s, %(max_lat)s, %(raw)s)
                ON CONFLICT (id) DO UPDATE SET
                    name = excluded.name, type = excluded.type,
                    min_lon = excluded.min_lon, min_lat = excluded.min_lat,
                    max_lon = excluded.max_lon, max_lat = excluded.max_lat,
                    raw = excluded.raw, updated_at = now()
                """,
                {**region, "raw": Jsonb(region["raw"])},
            )

    async def save_rubrics(self, rubrics: Iterable[Dict[str, Any]]) -> None:
        rows = [{**r, "raw": Jsonb(r.get("raw"))} for r in rubrics]
        if not rows:
            return
        async with self._lock, self._conn.transaction():
            async with self._conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO rubrics (id, parent_id, name, alias, type, raw)
                    VALUES (%(id)s, %(parent_id)s, %(name)s, %(alias)s, %(type)s, %(raw)s)
                    ON CONFLICT (id) DO UPDATE SET
                        parent_id = coalesce(excluded.parent_id, rubrics.parent_id),
                        name = excluded.name,
                        alias = coalesce(excluded.alias, rubrics.alias),
                        type = coalesce(excluded.type, rubrics.type),
                        raw = coalesce(excluded.raw, rubrics.raw),
                        updated_at = now()
                    """,
                    rows,
                )

    # ------------------------------------------------------------------ филиалы

    async def save_branches(self, branches: List[Dict[str, Any]]) -> int:
        """
        Сохраняет пачку филиалов в одной транзакции: организация, рубрики,
        сам филиал, связи с рубриками и контакты (контакты/рубрики перезаписываются).
        """
        if not branches:
            return 0

        orgs = {b["org"]["id"]: b["org"] for b in branches if b.get("org")}
        rubrics = {r["id"]: r for b in branches for r in b["rubrics"]}

        async with self._lock, self._conn.transaction():
            async with self._conn.cursor() as cur:
                if orgs:
                    await cur.executemany(
                        """
                        INSERT INTO organizations (id, name, branch_count, raw)
                        VALUES (%(id)s, %(name)s, %(branch_count)s, %(raw)s)
                        ON CONFLICT (id) DO UPDATE SET
                            name = excluded.name, branch_count = excluded.branch_count,
                            raw = excluded.raw, updated_at = now()
                        """,
                        [{**o, "raw": Jsonb(o["raw"])} for o in orgs.values()],
                    )
                if rubrics:
                    # Рубрики из карточки филиала неполные — не затираем данные рубрикатора
                    await cur.executemany(
                        """
                        INSERT INTO rubrics (id, parent_id, name, alias)
                        VALUES (%(id)s, %(parent_id)s, %(name)s, %(alias)s)
                        ON CONFLICT (id) DO UPDATE SET
                            parent_id = coalesce(rubrics.parent_id, excluded.parent_id),
                            alias = coalesce(rubrics.alias, excluded.alias)
                        """,
                        list(rubrics.values()),
                    )

                await cur.executemany(
                    """
                    INSERT INTO branches (
                        id, raw_id, org_id, region_id, name, name_primary, name_extension,
                        legal_name, address_name, full_address_name, address_comment,
                        postcode, building_id, city, district, lat, lon, rating,
                        review_count, org_rating, org_review_count, schedule, timezone,
                        attributes, flags, raw
                    ) VALUES (
                        %(id)s, %(raw_id)s, %(org_id)s, %(region_id)s, %(name)s,
                        %(name_primary)s, %(name_extension)s, %(legal_name)s,
                        %(address_name)s, %(full_address_name)s, %(address_comment)s,
                        %(postcode)s, %(building_id)s, %(city)s, %(district)s, %(lat)s,
                        %(lon)s, %(rating)s, %(review_count)s, %(org_rating)s,
                        %(org_review_count)s, %(schedule)s, %(timezone)s,
                        %(attributes)s, %(flags)s, %(raw)s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        raw_id = excluded.raw_id, org_id = excluded.org_id,
                        region_id = coalesce(excluded.region_id, branches.region_id),
                        name = excluded.name, name_primary = excluded.name_primary,
                        name_extension = excluded.name_extension,
                        legal_name = excluded.legal_name, address_name = excluded.address_name,
                        full_address_name = excluded.full_address_name,
                        address_comment = excluded.address_comment,
                        postcode = excluded.postcode, building_id = excluded.building_id,
                        city = excluded.city, district = excluded.district,
                        lat = excluded.lat, lon = excluded.lon, rating = excluded.rating,
                        review_count = excluded.review_count,
                        org_rating = excluded.org_rating,
                        org_review_count = excluded.org_review_count,
                        schedule = excluded.schedule, timezone = excluded.timezone,
                        attributes = excluded.attributes, flags = excluded.flags,
                        raw = excluded.raw, updated_at = now()
                    """,
                    [
                        {
                            **b,
                            "schedule": Jsonb(b["schedule"]),
                            "attributes": Jsonb(b["attributes"]),
                            "flags": Jsonb(b["flags"]),
                            "raw": Jsonb(b["raw"]),
                        }
                        for b in branches
                    ],
                )

                ids = [b["id"] for b in branches]
                await cur.execute("DELETE FROM branch_rubrics WHERE branch_id = ANY(%s)", (ids,))
                await cur.execute("DELETE FROM contacts WHERE branch_id = ANY(%s)", (ids,))

                links = [
                    {"branch_id": b["id"], "rubric_id": r["id"], "is_primary": r["is_primary"]}
                    for b in branches
                    for r in b["rubrics"]
                ]
                if links:
                    await cur.executemany(
                        """
                        INSERT INTO branch_rubrics (branch_id, rubric_id, is_primary)
                        VALUES (%(branch_id)s, %(rubric_id)s, %(is_primary)s)
                        ON CONFLICT DO NOTHING
                        """,
                        links,
                    )

                contacts = [{**c, "branch_id": b["id"]} for b in branches for c in b["contacts"]]
                if contacts:
                    await cur.executemany(
                        """
                        INSERT INTO contacts (branch_id, type, value, text, url, comment, position)
                        VALUES (%(branch_id)s, %(type)s, %(value)s, %(text)s, %(url)s,
                                %(comment)s, %(position)s)
                        ON CONFLICT DO NOTHING
                        """,
                        contacts,
                    )
        return len(branches)

    async def branches_for_reviews(
        self, region_id: Optional[int], only_missing: bool
    ) -> List[Dict[str, Any]]:
        """Филиалы, по которым нужно собрать отзывы (сначала никогда не синхронизированные)."""
        conditions = ["coalesce(review_count, 1) > 0"]
        params: Dict[str, Any] = {}
        if region_id is not None:
            conditions.append("region_id = %(region_id)s")
            params["region_id"] = region_id
        if only_missing:
            conditions.append("reviews_synced_at IS NULL")
        query = (
            "SELECT id, name, reviews_synced_at FROM branches WHERE "
            + " AND ".join(conditions)
            + " ORDER BY reviews_synced_at NULLS FIRST, id"
        )
        async with self._lock:
            cur = await self._conn.execute(query, params)
            return await cur.fetchall()

    # ------------------------------------------------------------------ отзывы

    async def known_review_ids(self, ids: List[str]) -> Set[str]:
        if not ids:
            return set()
        async with self._lock:
            cur = await self._conn.execute("SELECT id FROM reviews WHERE id = ANY(%s)", (ids,))
            return {row["id"] for row in await cur.fetchall()}

    async def save_reviews(self, reviews: List[Dict[str, Any]]) -> int:
        if not reviews:
            return 0
        async with self._lock, self._conn.transaction():
            async with self._conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO reviews (
                        id, branch_id, provider, rating, text, user_id, user_name,
                        user_reviews_count, likes_count, comments_count, photos_count,
                        is_verified, is_hidden, hiding_reason, official_answer_text,
                        official_answer_date, date_created, date_edited, url, raw
                    ) VALUES (
                        %(id)s, %(branch_id)s, %(provider)s, %(rating)s, %(text)s,
                        %(user_id)s, %(user_name)s, %(user_reviews_count)s,
                        %(likes_count)s, %(comments_count)s, %(photos_count)s,
                        %(is_verified)s, %(is_hidden)s, %(hiding_reason)s,
                        %(official_answer_text)s, %(official_answer_date)s,
                        %(date_created)s, %(date_edited)s, %(url)s, %(raw)s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        rating = excluded.rating, text = excluded.text,
                        user_name = excluded.user_name,
                        user_reviews_count = excluded.user_reviews_count,
                        likes_count = excluded.likes_count,
                        comments_count = excluded.comments_count,
                        photos_count = excluded.photos_count,
                        is_verified = excluded.is_verified, is_hidden = excluded.is_hidden,
                        hiding_reason = excluded.hiding_reason,
                        official_answer_text = excluded.official_answer_text,
                        official_answer_date = excluded.official_answer_date,
                        date_edited = excluded.date_edited, url = excluded.url,
                        raw = excluded.raw, updated_at = now()
                    """,
                    [{**r, "raw": Jsonb(r["raw"])} for r in reviews],
                )
        return len(reviews)

    async def mark_reviews_synced(self, branch_id: int) -> None:
        async with self._lock:
            await self._conn.execute(
                "UPDATE branches SET reviews_synced_at = now() WHERE id = %s", (branch_id,)
            )

    # ------------------------------------------------------------------ очередь обхода

    async def add_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        if not tasks:
            return
        rows = [
            {"rubric_id": None, "query": None, "depth": 0, **t} for t in tasks
        ]
        async with self._lock, self._conn.transaction():
            async with self._conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO crawl_tasks (region_id, rubric_id, query, min_lon, min_lat,
                                             max_lon, max_lat, depth)
                    VALUES (%(region_id)s, %(rubric_id)s, %(query)s, %(min_lon)s,
                            %(min_lat)s, %(max_lon)s, %(max_lat)s, %(depth)s)
                    ON CONFLICT DO NOTHING
                    """,
                    rows,
                )

    async def crawl_task_search_terms(self, region_id: int) -> List[str]:
        """
        Поисковые термины, использованные при обходе региона (текст запроса
        или название рубрики) — нужны браузерному фолбэку: сайт 2ГИС ищет по
        тексту в UI, а не по rubric_id/bbox, как Catalog API.
        """
        async with self._lock:
            cur = await self._conn.execute(
                """
                SELECT DISTINCT coalesce(t.query, r.name) AS term
                FROM crawl_tasks t
                LEFT JOIN rubrics r ON r.id = t.rubric_id
                WHERE t.region_id = %s AND coalesce(t.query, r.name) IS NOT NULL
                """,
                (region_id,),
            )
            rows = await cur.fetchall()
            return [row["term"] for row in rows if row["term"]]

    async def clear_tasks(self, region_id: int) -> int:
        """Удаляет очередь обхода региона (для повторного полного обхода)."""
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM crawl_tasks WHERE region_id = %s", (region_id,)
            )
            return cur.rowcount

    async def reset_running_tasks(self) -> int:
        """Возвращает в очередь задачи, оставшиеся 'running' после аварийного завершения."""
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE crawl_tasks SET status = 'pending', updated_at = now() "
                "WHERE status = 'running'"
            )
            return cur.rowcount

    async def claim_task(self, region_id: int) -> Optional[Dict[str, Any]]:
        async with self._lock:
            cur = await self._conn.execute(
                """
                UPDATE crawl_tasks SET status = 'running', attempts = attempts + 1,
                                       updated_at = now()
                WHERE id = (
                    SELECT id FROM crawl_tasks
                    WHERE status = 'pending' AND region_id = %s
                    ORDER BY depth, id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (region_id,),
            )
            return await cur.fetchone()

    async def finish_task(self, task_id: int, status: str, total: Optional[int],
                          fetched: Optional[int], error: Optional[str] = None) -> None:
        async with self._lock:
            await self._conn.execute(
                """
                UPDATE crawl_tasks SET status = %s, total = %s, fetched = %s,
                                       last_error = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, total, fetched, error, task_id),
            )

    async def task_stats(self, region_id: Optional[int] = None) -> Dict[str, int]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT status, count(*) AS n FROM crawl_tasks "
                "WHERE %(r)s::bigint IS NULL OR region_id = %(r)s GROUP BY status",
                {"r": region_id},
            )
            return {row["status"]: row["n"] for row in await cur.fetchall()}

    async def get_stats(self) -> Dict[str, int]:
        async with self._lock:
            cur = await self._conn.execute(
                """
                SELECT
                    (SELECT count(*) FROM regions) AS regions,
                    (SELECT count(*) FROM rubrics) AS rubrics,
                    (SELECT count(*) FROM organizations) AS organizations,
                    (SELECT count(*) FROM branches) AS branches,
                    (SELECT count(*) FROM contacts) AS contacts,
                    (SELECT count(*) FROM reviews) AS reviews
                """
            )
            return dict(await cur.fetchone())
