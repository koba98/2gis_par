"""
Хранилище данных 2ГИС в PostgreSQL (отдельная схема, по умолчанию `twogis`).
UPSERT рубрик, организаций, объектов (branches), контактов, отзывов и транспорта,
а также очередь задач браузерного обхода (web_crawl_tasks): у каждой задачи
хранится total (сколько объектов заявил 2ГИС) и collected (сколько собрано),
поэтому полноту сбора можно проверить запросом, а прерванный обход — продолжить.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "sql" / "schema.sql"


class SyncStorage:
    """
    Синхронный клиент PostgreSQL (psycopg 3) для работы с браузерным парсером.
    Не зависит от event loop и может безопасно вызываться из синхронного кода Playwright.
    """

    def __init__(self, dsn: Optional[str] = None, schema: str = "twogis"):
        self.dsn = dsn
        self.schema = schema
        self._conn: Optional[psycopg.Connection] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.dsn and self.dsn.strip())

    def connect(self) -> bool:
        if not self.is_configured:
            logger.info("PG_DSN не задан — запись в PostgreSQL невозможна.")
            return False
        if self._conn is None:
            self._conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
            self._conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            logger.info("Синхронное подключение к PostgreSQL установлено (схема: %s).", self.schema)
        return True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def init_db(self) -> None:
        if not self.connect():
            return
        ddl = SCHEMA_FILE.read_text(encoding="utf-8")
        with self._conn.transaction():
            # несколько воркеров стартуют одновременно: DDL выполняется по очереди
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('twogis_init_db'))")
            self._conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            self._conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
            self._conn.execute(ddl)
        logger.info("Схема '%s' инициализирована (sync).", self.schema)

    def save_rubrics(self, rubrics: Iterable[Dict[str, Any]]) -> None:
        if not self.connect() or not rubrics:
            return
        rows = [{"alias": None, "type": None, **r, "raw": Jsonb(r.get("raw"))} for r in rubrics]
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO rubrics (id, parent_id, name, alias, type, raw)
                    VALUES (%(id)s, %(parent_id)s, %(name)s, %(alias)s, %(type)s, %(raw)s)
                    ON CONFLICT (id) DO UPDATE SET
                        parent_id = coalesce(excluded.parent_id, rubrics.parent_id),
                        name = excluded.name,
                        type = coalesce(excluded.type, rubrics.type),
                        raw = coalesce(excluded.raw, rubrics.raw),
                        updated_at = now()
                    """,
                    rows,
                )

    def save_branches(self, branches: List[Dict[str, Any]], full_card: bool = False) -> int:
        """
        full_card=True — данные со страницы карточки (контакты, соцсети, всё остальное):
        перезаписывают объект целиком, ставят card_synced_at.
        full_card=False — данные из поисковой выдачи / «В здании»: новый объект
        вставляется, а у объекта с уже собранной полной карточкой ничего не затирается.
        """
        if not self.connect() or not branches:
            return 0
        orgs = {b["org"]["id"]: b["org"] for b in branches if b.get("org")}
        rubrics = {r["id"]: r for b in branches for r in b.get("rubrics", [])}
        rows = [
            {
                **b,
                "rubrics": b.get("rubrics_names") or None,
                "schedule": Jsonb(b.get("schedule")),
                "attributes": Jsonb(b.get("attributes")),
                "flags": Jsonb(b.get("flags")),
                "raw": Jsonb(b.get("raw")),
                "full_card": full_card,
            }
            for b in branches
        ]
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                if orgs:
                    cur.executemany(
                        """
                        INSERT INTO organizations (id, name, branch_count, raw)
                        VALUES (%(id)s, %(name)s, %(branch_count)s, %(raw)s)
                        ON CONFLICT (id) DO UPDATE SET
                            name = coalesce(excluded.name, organizations.name),
                            branch_count = coalesce(excluded.branch_count, organizations.branch_count),
                            raw = excluded.raw, updated_at = now()
                        """,
                        [{**o, "raw": Jsonb(o.get("raw"))} for o in orgs.values()],
                    )
                if rubrics:
                    cur.executemany(
                        """
                        INSERT INTO rubrics (id, parent_id, name, alias)
                        VALUES (%(id)s, %(parent_id)s, %(name)s, %(alias)s)
                        ON CONFLICT (id) DO UPDATE SET
                            parent_id = coalesce(rubrics.parent_id, excluded.parent_id),
                            alias = coalesce(rubrics.alias, excluded.alias)
                        """,
                        list(rubrics.values()),
                    )
                cur.executemany(
                    """
                    INSERT INTO branches (
                        id, raw_id, org_id, region_id, name, name_primary, name_extension,
                        legal_name, address_name, full_address_name, address_comment,
                        postcode, building_id, city, district, lat, lon, rating,
                        review_count, org_rating, org_review_count, primary_rubric, rubrics,
                        schedule, timezone, attributes, flags, raw, card_synced_at
                    ) VALUES (
                        %(id)s, %(raw_id)s, %(org_id)s, %(region_id)s, %(name)s,
                        %(name_primary)s, %(name_extension)s, %(legal_name)s,
                        %(address_name)s, %(full_address_name)s, %(address_comment)s,
                        %(postcode)s, %(building_id)s, %(city)s, %(district)s, %(lat)s,
                        %(lon)s, %(rating)s, %(review_count)s, %(org_rating)s,
                        %(org_review_count)s, %(primary_rubric)s, %(rubrics)s,
                        %(schedule)s, %(timezone)s, %(attributes)s, %(flags)s, %(raw)s,
                        CASE WHEN %(full_card)s THEN now() END
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        raw_id = excluded.raw_id,
                        org_id = coalesce(excluded.org_id, branches.org_id),
                        region_id = coalesce(excluded.region_id, branches.region_id),
                        name = excluded.name, name_primary = excluded.name_primary,
                        name_extension = excluded.name_extension,
                        legal_name = excluded.legal_name, address_name = excluded.address_name,
                        full_address_name = excluded.full_address_name,
                        address_comment = excluded.address_comment,
                        postcode = excluded.postcode,
                        building_id = coalesce(excluded.building_id, branches.building_id),
                        city = coalesce(excluded.city, branches.city),
                        district = coalesce(excluded.district, branches.district),
                        lat = excluded.lat, lon = excluded.lon, rating = excluded.rating,
                        review_count = excluded.review_count,
                        org_rating = excluded.org_rating,
                        org_review_count = excluded.org_review_count,
                        primary_rubric = coalesce(excluded.primary_rubric, branches.primary_rubric),
                        rubrics = coalesce(excluded.rubrics, branches.rubrics),
                        schedule = excluded.schedule, timezone = excluded.timezone,
                        attributes = excluded.attributes, flags = excluded.flags,
                        raw = excluded.raw,
                        card_synced_at = coalesce(excluded.card_synced_at, branches.card_synced_at),
                        updated_at = now()
                    WHERE excluded.card_synced_at IS NOT NULL OR branches.card_synced_at IS NULL
                    """,
                    rows,
                )
                ids = [b["id"] for b in branches]
                if full_card:
                    cur.execute("DELETE FROM branch_rubrics WHERE branch_id = ANY(%s)", (ids,))
                    cur.execute("DELETE FROM contacts WHERE branch_id = ANY(%s)", (ids,))
                links = [
                    {"branch_id": b["id"], "rubric_id": r["id"], "is_primary": r.get("is_primary", False)}
                    for b in branches
                    for r in b.get("rubrics", [])
                ]
                if links:
                    cur.executemany(
                        """
                        INSERT INTO branch_rubrics (branch_id, rubric_id, is_primary)
                        VALUES (%(branch_id)s, %(rubric_id)s, %(is_primary)s)
                        ON CONFLICT (branch_id, rubric_id) DO NOTHING
                        """,
                        links,
                    )
                contacts = [{"branch_id": b["id"], **c} for b in branches for c in b.get("contacts", [])]
                if full_card and contacts:
                    cur.executemany(
                        """
                        INSERT INTO contacts (branch_id, type, value, text, url, comment, position)
                        VALUES (%(branch_id)s, %(type)s, %(value)s, %(text)s, %(url)s, %(comment)s, %(position)s)
                        """,
                        contacts,
                    )
        return len(branches)

    _DETAIL_COLUMNS = """
        id, coalesce(raw->>'type', 'branch') AS type, name, review_count, card_synced_at, reviews_synced_at
    """

    def branches_without_card(self, ids: Sequence[int]) -> List[Dict[str, Any]]:
        """Какие из объектов ещё без полной карточки."""
        if not self.connect() or not ids:
            return []
        cur = self._conn.execute(
            f"""
            SELECT {self._DETAIL_COLUMNS} FROM branches
            WHERE id = ANY(%s) AND card_synced_at IS NULL AND card_attempts < 3 ORDER BY id
            """,
            (list(ids),),
        )
        return list(cur.fetchall())

    # что осталось собрать по объекту: полная карточка и/или лента отзывов
    _NEEDS = {
        "cards": "card_synced_at IS NULL AND card_attempts < 3",
        "reviews": "coalesce(review_count, 0) > 0 AND reviews_synced_at IS NULL AND reviews_attempts < 3",
    }
    _NEEDS["details"] = f"(({_NEEDS['cards']}) OR ({_NEEDS['reviews']}))"

    def objects_for_details(self, region_id: int, main_city: Optional[str], limit: Optional[int],
                            need: str = "details", shard: Tuple[int, int] = (0, 1)) -> List[Dict[str, Any]]:
        """
        Объекты проекта (город + населённые пункты-спутники), которым не хватает карточки или отзывов.
        Сначала сам город, потом спутники; внутри — объекты с большим числом отзывов первыми.
        shard=(i, n) — доля i из n для параллельных процессов.
        """
        if not self.connect():
            return []
        cur = self._conn.execute(
            f"""
            SELECT {self._DETAIL_COLUMNS} FROM branches
            WHERE region_id = %(region)s AND {self._NEEDS[need]} AND id %% %(n)s = %(i)s
            ORDER BY (city IS DISTINCT FROM %(city)s), coalesce(review_count, 0) DESC, id
            LIMIT %(limit)s
            """,
            {"region": region_id, "city": main_city, "limit": limit, "n": shard[1], "i": shard[0]},
        )
        return list(cur.fetchall())

    def count_objects_for_details(self, region_id: int, need: str = "details") -> int:
        if not self.connect():
            return 0
        return self._conn.execute(
            f"SELECT count(*) AS n FROM branches WHERE region_id = %s AND {self._NEEDS[need]}", (region_id,)
        ).fetchone()["n"]

    def count_in_building(self, building_id: int) -> int:
        """Сколько объектов с этим building_id уже в базе (для сверки с total вкладки «В здании»)."""
        if not self.connect():
            return 0
        return self._conn.execute("SELECT count(*) AS n FROM branches WHERE building_id = %s AND id <> %s",
                                  (building_id, building_id)).fetchone()["n"]

    def mark_card_synced(self, ids: Sequence[int]) -> None:
        if self.connect() and ids:
            self._conn.execute("UPDATE branches SET card_synced_at = now() WHERE id = ANY(%s)", (list(ids),))

    def mark_card_failed(self, branch_id: int) -> None:
        if self.connect():
            self._conn.execute("UPDATE branches SET card_attempts = card_attempts + 1 WHERE id = %s", (branch_id,))

    def branches_by_ids(self, ids: Sequence[int]) -> List[Dict[str, Any]]:
        if not self.connect() or not ids:
            return []
        cur = self._conn.execute(
            """
            SELECT id, coalesce(raw->>'type', 'branch') AS type, name, review_count,
                   building_id, lat, lon, reviews_synced_at, card_synced_at
            FROM branches WHERE id = ANY(%s)
            """,
            (list(ids),),
        )
        return list(cur.fetchall())

    def save_review_comments(self, comments: List[Dict[str, Any]]) -> int:
        if not self.connect() or not comments:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO review_comments (
                    id, review_id, branch_id, text, is_official_answer, author_name,
                    date_created, is_hidden, raw
                ) VALUES (
                    %(id)s, %(review_id)s, %(branch_id)s, %(text)s, %(is_official_answer)s,
                    %(author_name)s, %(date_created)s, %(is_hidden)s, %(raw)s
                )
                ON CONFLICT (id) DO UPDATE SET
                    text = excluded.text, is_hidden = excluded.is_hidden, raw = excluded.raw
                """,
                [{**c, "raw": Jsonb(c["raw"])} for c in comments],
            )
        return len(comments)

    def save_reviews(self, reviews: List[Dict[str, Any]]) -> int:
        if not self.connect() or not reviews:
            return 0
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
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

    def mark_reviews_synced(self, branch_id: int) -> None:
        if self.connect():
            self._conn.execute("UPDATE branches SET reviews_synced_at = now() WHERE id = %s", (branch_id,))

    def mark_reviews_incomplete(self, branch_id: int) -> None:
        if self.connect():
            self._conn.execute("UPDATE branches SET reviews_attempts = reviews_attempts + 1 WHERE id = %s",
                               (branch_id,))

    def save_transport_stops(self, stops: List[Dict[str, Any]]) -> int:
        if not self.connect() or not stops:
            return 0
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO transport_stops (
                        id, region_id, region, city, city_slug, name, type, subtype,
                        lat, lon, district, raw
                    ) VALUES (
                        %(id)s, %(region_id)s, %(region)s, %(city)s, %(city_slug)s,
                        %(name)s, %(type)s, %(subtype)s, %(lat)s, %(lon)s, %(district)s, %(raw)s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        region = coalesce(excluded.region, transport_stops.region),
                        city = coalesce(excluded.city, transport_stops.city),
                        city_slug = excluded.city_slug,
                        name = excluded.name,
                        type = coalesce(excluded.type, transport_stops.type),
                        subtype = coalesce(excluded.subtype, transport_stops.subtype),
                        lat = coalesce(excluded.lat, transport_stops.lat),
                        lon = coalesce(excluded.lon, transport_stops.lon),
                        district = coalesce(excluded.district, transport_stops.district),
                        raw = coalesce(excluded.raw, transport_stops.raw),
                        scraped_at = now()
                    """,
                    [
                        {
                            "id": str(s["id"]),
                            "region_id": s.get("region_id"),
                            "region": s.get("region"),
                            "city": s.get("city"),
                            "city_slug": s.get("city_slug"),
                            "name": s["name"],
                            "type": s.get("type", "station"),
                            "subtype": s.get("subtype"),
                            "lat": s.get("lat"),
                            "lon": s.get("lon"),
                            "district": s.get("district"),
                            "raw": Jsonb(s.get("raw")),
                        }
                        for s in stops
                    ],
                )
        return len(stops)

    def save_transport_routes(self, routes: List[Dict[str, Any]]) -> int:
        if not self.connect() or not routes:
            return 0
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO transport_routes (
                        id, city_slug, region_id, name, subtype, from_name, to_name, color, raw
                    ) VALUES (
                        %(id)s, %(city_slug)s, %(region_id)s, %(name)s, %(subtype)s,
                        %(from_name)s, %(to_name)s, %(color)s, %(raw)s
                    )
                    ON CONFLICT (city_slug, id) DO UPDATE SET
                        name = excluded.name,
                        subtype = excluded.subtype,
                        from_name = coalesce(excluded.from_name, transport_routes.from_name),
                        to_name = coalesce(excluded.to_name, transport_routes.to_name),
                        color = coalesce(excluded.color, transport_routes.color),
                        raw = coalesce(excluded.raw, transport_routes.raw),
                        scraped_at = now()
                    """,
                    [
                        {
                            "id": str(r["id"]),
                            "city_slug": r.get("city_slug"),
                            "region_id": r.get("region_id"),
                            "name": str(r["name"]),
                            "subtype": r.get("subtype", "bus"),
                            "from_name": r.get("from_name"),
                            "to_name": r.get("to_name"),
                            "color": r.get("color"),
                            "raw": Jsonb(r.get("raw")),
                        }
                        for r in routes
                    ],
                )
        return len(routes)

    def save_transport_route_stops(self, rows: List[Dict[str, Any]]) -> int:
        if not self.connect() or not rows:
            return 0
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO transport_route_stops (
                        region, city, city_slug, route_id, route_number, route_subtype,
                        route_from, route_to, stop_id, stop_name, lat, lon, district, color
                    ) VALUES (
                        %(region)s, %(city)s, %(city_slug)s, %(route_id)s, %(route_number)s,
                        %(route_subtype)s, %(route_from)s, %(route_to)s, %(stop_id)s,
                        %(stop_name)s, %(lat)s, %(lon)s, %(district)s, %(color)s
                    )
                    ON CONFLICT (city_slug, route_id, stop_id) DO UPDATE SET
                        region = coalesce(excluded.region, transport_route_stops.region),
                        city = coalesce(excluded.city, transport_route_stops.city),
                        route_number = excluded.route_number,
                        route_subtype = excluded.route_subtype,
                        route_from = coalesce(excluded.route_from, transport_route_stops.route_from),
                        route_to = coalesce(excluded.route_to, transport_route_stops.route_to),
                        stop_name = excluded.stop_name,
                        lat = excluded.lat,
                        lon = excluded.lon,
                        district = coalesce(excluded.district, transport_route_stops.district),
                        color = coalesce(excluded.color, transport_route_stops.color),
                        scraped_at = now()
                    """,
                    [
                        {
                            "region": r.get("region"),
                            "city": r.get("city"),
                            "city_slug": r.get("city_slug"),
                            "route_id": str(r["route_id"]),
                            "route_number": str(r.get("route_number") or ""),
                            "route_subtype": r.get("route_subtype", "bus"),
                            "route_from": r.get("route_from"),
                            "route_to": r.get("route_to"),
                            "stop_id": str(r["stop_id"]),
                            "stop_name": r.get("stop_name"),
                            "lat": r.get("lat"),
                            "lon": r.get("lon"),
                            "district": r.get("district"),
                            "color": r.get("color"),
                        }
                        for r in rows
                    ],
                )
        return len(rows)

    def stops_without_address(self, city_slug: str) -> List[Dict[str, Any]]:
        if not self.connect():
            return []
        cur = self._conn.execute(
            """
            SELECT id, name FROM transport_stops
            WHERE city_slug = %s AND (raw IS NULL OR raw->'adm_div' IS NULL) ORDER BY id
            """,
            (city_slug,),
        )
        return list(cur.fetchall())

    def routes_without_platforms(self, city_slug: str) -> List[Dict[str, Any]]:
        """Маршруты из БД, у которых ещё нет остановок по порядку (в том числе найденные прошлыми запусками)."""
        if not self.connect():
            return []
        cur = self._conn.execute(
            """
            SELECT r.id, r.name, r.subtype, r.color, r.raw FROM transport_routes r
            WHERE r.city_slug = %s AND r.platforms_synced_at IS NULL
            ORDER BY r.subtype, r.name
            """,
            (city_slug,),
        )
        return list(cur.fetchall())

    def mark_route_platforms_synced(self, city_slug: str, route_id: str) -> None:
        if self.connect():
            self._conn.execute("UPDATE transport_routes SET platforms_synced_at = now() WHERE city_slug = %s AND id = %s",
                               (city_slug, route_id))

    def save_route_platforms(self, city_slug: str, route_id: str, platforms: List[Dict[str, Any]]) -> None:
        """Перезаписывает остановки маршрута по порядку (по всем направлениям)."""
        if not self.connect():
            return
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM transport_route_platforms WHERE city_slug = %s AND route_id = %s",
                            (city_slug, route_id))
                if platforms:
                    cur.executemany(
                        """
                        INSERT INTO transport_route_platforms (
                            city_slug, route_id, direction_no, direction_type, seq,
                            platform_id, stop_id, stop_name, lat, lon
                        ) VALUES (
                            %(city_slug)s, %(route_id)s, %(direction_no)s, %(direction_type)s, %(seq)s,
                            %(platform_id)s, %(stop_id)s, %(stop_name)s, %(lat)s, %(lon)s
                        )
                        """,
                        platforms,
                    )

    def save_missing_transport_stops(self, stops: List[Dict[str, Any]]) -> None:
        """Добавляет остановки, которых не было в поиске; существующие не трогает."""
        if not self.connect() or not stops:
            return
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO transport_stops (id, region, city, city_slug, name, type, subtype, lat, lon)
                VALUES (%(id)s, %(region)s, %(city)s, %(city_slug)s, %(name)s, %(type)s, %(subtype)s, %(lat)s, %(lon)s)
                ON CONFLICT (id) DO NOTHING
                """,
                stops,
            )

    def merge_stop(self, city_slug: str, keep_id: str, dup_id: str) -> None:
        """Сливает дубль остановки в keep_id: связи с маршрутами переносятся, дубль удаляется."""
        if not self.connect() or keep_id == dup_id:
            return
        with self._conn.transaction():
            self._conn.execute(
                """
                DELETE FROM transport_route_stops d WHERE d.city_slug = %(c)s AND d.stop_id = %(dup)s
                  AND EXISTS (SELECT 1 FROM transport_route_stops k
                              WHERE k.city_slug = d.city_slug AND k.route_id = d.route_id AND k.stop_id = %(keep)s)
                """,
                {"c": city_slug, "keep": keep_id, "dup": dup_id},
            )
            self._conn.execute("UPDATE transport_route_stops SET stop_id = %s WHERE city_slug = %s AND stop_id = %s",
                               (keep_id, city_slug, dup_id))
            self._conn.execute("UPDATE transport_route_platforms SET stop_id = %s WHERE city_slug = %s AND stop_id = %s",
                               (keep_id, city_slug, dup_id))
            self._conn.execute("DELETE FROM transport_stops WHERE id = %s AND city_slug = %s", (dup_id, city_slug))

    def merge_redirected_stops(self, city_slug: str) -> int:
        """
        Остановки, чья карточка — другой объект (станция LRT = организация «…, станция Tarlan Astana»),
        и эта же станция, сохранённая из поиска под id организации: дубль сливается в остановку маршрута.
        """
        if not self.connect():
            return 0
        pairs = self._conn.execute(
            """
            SELECT s.id AS keep_id, d.id AS dup_id FROM transport_stops s
            JOIN transport_stops d ON d.city_slug = s.city_slug AND d.id = split_part(s.raw->>'id', '_', 1)
            WHERE s.city_slug = %s AND d.id <> s.id
            """,
            (city_slug,),
        ).fetchall()
        for p in pairs:
            self.merge_stop(city_slug, p["keep_id"], p["dup_id"])
        return len(pairs)

    _EXPORTS = {
        "stops": "SELECT id, name, subtype, lat, lon, region, district_area, locality, district, microdistrict "
                 "FROM v_transport_stops WHERE city_slug = %s ORDER BY locality, name",
        "routes": "SELECT id, name, subtype, from_name, to_name, color FROM transport_routes "
                  "WHERE city_slug = %s ORDER BY subtype, name",
        "platforms": "SELECT route_type, route_number, route_from, route_to, route_id, direction_no, "
                     "direction_type, seq, stop_id, stop_name, lat, lon, locality, district, microdistrict "
                     "FROM v_route_stops_ordered WHERE city_slug = %s "
                     "ORDER BY route_type, route_number, direction_no, seq",
    }

    def transport_export(self, city_slug: str, kind: str) -> List[Dict[str, Any]]:
        if not self.connect():
            return []
        return list(self._conn.execute(self._EXPORTS[kind], (city_slug,)).fetchall())

    def get_stats(self) -> Dict[str, int]:
        if not self.connect():
            return {}
        cur = self._conn.execute(
            """
            SELECT
                (SELECT count(*) FROM web_crawl_tasks) AS web_crawl_tasks,
                (SELECT count(*) FROM rubrics) AS rubrics,
                (SELECT count(*) FROM organizations) AS organizations,
                (SELECT count(*) FROM branches) AS branches,
                (SELECT count(*) FROM branches WHERE card_synced_at IS NOT NULL) AS branches_full_card,
                (SELECT count(*) FROM contacts) AS contacts,
                (SELECT count(*) FROM reviews) AS reviews,
                (SELECT count(*) FROM review_comments) AS review_comments,
                (SELECT count(*) FROM transport_stops) AS transport_stops,
                (SELECT count(*) FROM transport_routes) AS transport_routes,
                (SELECT count(*) FROM transport_route_stops) AS transport_route_stops
            """
        )
        return dict(cur.fetchone())

    def get_category_stats(self, city: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Группировка всех объектов (организации, территории, транспорт) по категориям."""
        if not self.connect():
            return []
        cur = self._conn.execute(
            """
            SELECT category, count(*) AS count
            FROM v_all_objects
            WHERE %(city)s::text IS NULL OR city = %(city)s
            GROUP BY category
            ORDER BY count(*) DESC
            LIMIT %(limit)s
            """,
            {"city": city, "limit": limit},
        )
        return list(cur.fetchall())

    # ------------------------------------------------------------------ очередь обхода

    def add_web_tasks(self, city_slug: str, kind: str, entries: Iterable[Tuple[str, Optional[str]]]) -> None:
        """Добавляет задачи (key, label); уже известные задачи не дублируются."""
        rows = [{"city_slug": city_slug, "kind": kind, "key": str(k), "label": label} for k, label in entries]
        if not rows or not self.connect():
            return
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO web_crawl_tasks (city_slug, kind, key, label)
                VALUES (%(city_slug)s, %(kind)s, %(key)s, %(label)s)
                ON CONFLICT (city_slug, kind, key) DO NOTHING
                """,
                rows,
            )

    def clear_web_tasks(self, city_slug: str) -> int:
        if not self.connect():
            return 0
        cur = self._conn.execute("DELETE FROM web_crawl_tasks WHERE city_slug = %s", (city_slug,))
        return cur.rowcount

    def reset_web_tasks(self, city_slug: str, max_attempts: int) -> int:
        """Возвращает в очередь прерванные задачи и незавершённые, у которых остались попытки."""
        if not self.connect():
            return 0
        cur = self._conn.execute(
            """
            UPDATE web_crawl_tasks SET status = 'pending', updated_at = now()
            WHERE city_slug = %s AND kind NOT IN ('transport', 'area')
              AND ((status = 'running' AND updated_at < now() - interval '30 minutes')
                   OR (status IN ('incomplete', 'error') AND attempts < %s))
            """,
            (city_slug, max_attempts),
        )
        return cur.rowcount

    def claim_web_task(self, city_slug: str, kinds: Sequence[str]) -> Optional[Dict[str, Any]]:
        """Берёт следующую задачу: сначала запросы, потом рубрики, потом здания."""
        if not self.connect():
            return None
        cur = self._conn.execute(
            """
            UPDATE web_crawl_tasks SET status = 'running', attempts = attempts + 1, updated_at = now()
            WHERE id = (
                SELECT id FROM web_crawl_tasks
                WHERE city_slug = %(city)s AND status = 'pending' AND kind = ANY(%(kinds)s)
                ORDER BY CASE kind WHEN 'query' THEN 0 WHEN 'rubric' THEN 1 ELSE 2 END, id
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            RETURNING id, kind, key, label, attempts
            """,
            {"city": city_slug, "kinds": list(kinds)},
        )
        return cur.fetchone()

    def claim_web_task_by_key(self, city_slug: str, kind: str, key: str, max_attempts: int
                              ) -> Optional[Dict[str, Any]]:
        """Берёт конкретную задачу, если она ещё не выполнена (и попытки не исчерпаны)."""
        if not self.connect():
            return None
        cur = self._conn.execute(
            """
            UPDATE web_crawl_tasks SET status = 'running', attempts = attempts + 1, updated_at = now()
            WHERE city_slug = %s AND kind = %s AND key = %s
              AND (status IN ('pending', 'running') OR (status IN ('incomplete', 'error') AND attempts < %s))
            RETURNING id, kind, key, label, attempts
            """,
            (city_slug, kind, str(key), max_attempts),
        )
        return cur.fetchone()

    def branch_ids_in_buildings(self, building_ids: Sequence[int]) -> List[int]:
        if not self.connect() or not building_ids:
            return []
        cur = self._conn.execute("SELECT id FROM branches WHERE building_id = ANY(%s)", (list(building_ids),))
        return [r["id"] for r in cur.fetchall()]

    def set_web_task_result(self, city_slug: str, kind: str, key: str, status: str,
                            total: Optional[int], collected: Optional[int]) -> None:
        if self.connect():
            self._conn.execute(
                """
                UPDATE web_crawl_tasks SET status = %s, total = %s, collected = %s,
                    attempts = attempts + 1, updated_at = now()
                WHERE city_slug = %s AND kind = %s AND key = %s
                """,
                (status, total, collected, city_slug, kind, str(key)),
            )

    def web_task_done(self, city_slug: str, kind: str, key: str) -> bool:
        if not self.connect():
            return False
        return self._conn.execute(
            "SELECT 1 FROM web_crawl_tasks WHERE city_slug = %s AND kind = %s AND key = %s AND status = 'done'",
            (city_slug, kind, key),
        ).fetchone() is not None

    def open_web_tasks(self, city_slug: str, kinds: Sequence[str], max_attempts: int) -> int:
        """Задачи, которые ещё будут выполняться: в очереди, в работе или неполные с оставшимися попытками."""
        if not self.connect():
            return 0
        return self._conn.execute(
            """
            SELECT count(*) AS n FROM web_crawl_tasks
            WHERE city_slug = %s AND kind = ANY(%s)
              AND (status IN ('pending', 'running') OR (status IN ('incomplete', 'error') AND attempts < %s))
            """,
            (city_slug, list(kinds), max_attempts),
        ).fetchone()["n"]

    def reset_exhausted(self, city_slug: str, region_id: int) -> Dict[str, int]:
        """Добор: даёт ещё попытки задачам и объектам, у которых они кончились (неполные, с ошибкой)."""
        if not self.connect():
            return {}
        with self._conn.transaction():
            tasks = self._conn.execute(
                """
                UPDATE web_crawl_tasks SET status = 'pending', attempts = 0, updated_at = now()
                WHERE city_slug = %s AND kind IN ('query', 'rubric', 'building') AND status IN ('incomplete', 'error')
                """,
                (city_slug,),
            ).rowcount
            cards = self._conn.execute(
                "UPDATE branches SET card_attempts = 0 WHERE region_id = %s AND card_synced_at IS NULL AND card_attempts >= 3",
                (region_id,),
            ).rowcount
            reviews = self._conn.execute(
                """
                UPDATE branches SET reviews_attempts = 0
                WHERE region_id = %s AND reviews_synced_at IS NULL AND reviews_attempts >= 3
                """,
                (region_id,),
            ).rowcount
        return {"задач": tasks, "карточек": cards, "лент отзывов": reviews}

    # ------------------------------------------------------------------ проекты 2ГИС (регионы)

    def region_fresh(self, region_id: int, days: int = 7) -> bool:
        if not self.connect():
            return False
        return self._conn.execute(
            """
            SELECT 1 FROM regions
            WHERE id = %s AND updated_at > now() - make_interval(days => %s)
              AND city_slug IS NOT NULL AND raw ? 'statistics'
            """,
            (region_id, days),
        ).fetchone() is not None

    def save_region(self, region_id: int, city_slug: str, data: Dict[str, Any],
                    rings: List[List[Tuple[float, float]]]) -> None:
        if not self.connect():
            return
        lons = [p[0] for r in rings for p in r] or [None]
        lats = [p[1] for r in rings for p in r] or [None]
        self._conn.execute(
            """
            INSERT INTO regions (id, city_slug, name, type, min_lon, min_lat, max_lon, max_lat, raw)
            VALUES (%(id)s, %(slug)s, %(name)s, %(type)s, %(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, %(raw)s)
            ON CONFLICT (id) DO UPDATE SET
                city_slug = excluded.city_slug, name = excluded.name, type = excluded.type,
                min_lon = excluded.min_lon, min_lat = excluded.min_lat,
                max_lon = excluded.max_lon, max_lat = excluded.max_lat,
                raw = excluded.raw, updated_at = now()
            """,
            {
                "id": region_id, "slug": city_slug, "name": data.get("name") or "", "type": data.get("type"),
                "min_lon": min(lons) if lons[0] is not None else None,
                "min_lat": min(lats) if lats[0] is not None else None,
                "max_lon": max(lons) if lons[0] is not None else None,
                "max_lat": max(lats) if lats[0] is not None else None,
                "raw": Jsonb(data),
            },
        )

    def completeness(self) -> List[Dict[str, Any]]:
        """Полнота по городам: заявлено 2ГИС (statistics проекта) / собрано, карточки, отзывы, очередь."""
        if not self.connect():
            return []
        return list(self._conn.execute("SELECT * FROM v_city_completeness ORDER BY declared_branches DESC NULLS LAST"))

    # ------------------------------------------------------------------ план обхода kz: город × этап

    def init_plan(self, jobs: Iterable[Tuple[str, str, int, int]]) -> None:
        """Добавляет в план (город, этап, волна, порядок); уже известные задачи сохраняют статус."""
        rows = [{"city": c, "stage": s, "tier": t, "position": p} for c, s, t, p in jobs]
        if not rows or not self.connect():
            return
        with self._conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO crawl_plan (city_slug, stage, tier, position)
                VALUES (%(city)s, %(stage)s, %(tier)s, %(position)s)
                ON CONFLICT (city_slug, stage) DO UPDATE SET tier = excluded.tier, position = excluded.position
                """,
                rows,
            )

    def claim_plan_job(self, tiers: Sequence[int], worker: str, stale_minutes: int = 45) -> Optional[Dict[str, Any]]:
        """
        Следующий этап по порядку плана. Этап города берётся, только когда все предыдущие этапы
        этого города выполнены; этапы, «зависшие» у упавшего воркера, возвращаются в работу.
        Несколько воркеров (с разными IP) берут разные этапы — FOR UPDATE SKIP LOCKED.
        """
        if not self.connect():
            return None
        with self._conn.transaction():
            self._conn.execute(
                """
                UPDATE crawl_plan SET status = 'pending', worker = NULL
                WHERE status = 'running' AND updated_at < now() - make_interval(mins => %s)
                """,
                (stale_minutes,),
            )
            return self._conn.execute(
                """
                UPDATE crawl_plan SET status = 'running', worker = %(worker)s, attempts = attempts + 1,
                    started_at = coalesce(started_at, now()), updated_at = now()
                WHERE (city_slug, stage) = (
                    SELECT p.city_slug, p.stage FROM crawl_plan p
                    WHERE p.status = 'pending' AND p.tier = ANY(%(tiers)s)
                      AND (p.not_before IS NULL OR p.not_before <= now())
                      AND NOT EXISTS (
                          SELECT 1 FROM crawl_plan q
                          WHERE q.city_slug = p.city_slug AND q.position < p.position AND q.status <> 'done')
                    ORDER BY p.position
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                RETURNING city_slug, stage, tier, attempts
                """,
                {"tiers": list(tiers), "worker": worker},
            ).fetchone()

    def finish_plan_job(self, city_slug: str, stage: str, status: str, error: Optional[str] = None,
                        delay_minutes: int = 0) -> None:
        """status: done — этап выполнен; pending — вернуть в очередь (через delay_minutes)."""
        if self.connect():
            self._conn.execute(
                """
                UPDATE crawl_plan SET status = %s, last_error = %s, worker = NULL, updated_at = now(),
                    finished_at = CASE WHEN %s = 'done' THEN now() END,
                    not_before = now() + make_interval(mins => %s)
                WHERE city_slug = %s AND stage = %s
                """,
                (status, error, status, delay_minutes, city_slug, stage),
            )

    def touch_plan_job(self, city_slug: str, stage: str) -> None:
        if self.connect():
            self._conn.execute("UPDATE crawl_plan SET updated_at = now() WHERE city_slug = %s AND stage = %s",
                               (city_slug, stage))

    def plan_status(self) -> List[Dict[str, Any]]:
        if not self.connect():
            return []
        return list(self._conn.execute(
            """
            SELECT tier, city_slug, stage, status, attempts, worker,
                   started_at::timestamp(0) AS started, finished_at::timestamp(0) AS finished,
                   left(last_error, 80) AS error
            FROM crawl_plan ORDER BY position
            """
        ))

    def touch_web_task(self, task_id: int) -> None:
        """Отметка «задача жива» для длинных задач (сотни страниц), чтобы её не сбросили как зависшую."""
        if self.connect():
            self._conn.execute("UPDATE web_crawl_tasks SET updated_at = now() WHERE id = %s", (task_id,))

    def finish_web_task(
        self,
        task_id: int,
        status: str,
        total: Optional[int] = None,
        collected: Optional[int] = None,
        pages_total: Optional[int] = None,
        pages_done: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        if not self.connect():
            return
        self._conn.execute(
            """
            UPDATE web_crawl_tasks SET status = %s, total = %s, collected = %s,
                pages_total = %s, pages_done = %s, last_error = %s, updated_at = now()
            WHERE id = %s
            """,
            (status, total, collected, pages_total, pages_done, error, task_id),
        )

    def web_task_stats(self, city_slug: Optional[str] = None) -> List[Dict[str, Any]]:
        """Покрытие обхода по типу и статусу задач: заявлено 2ГИС (total) и собрано (collected)."""
        if not self.connect():
            return []
        cur = self._conn.execute(
            """
            SELECT city_slug, kind, status, count(*) AS tasks,
                   coalesce(sum(total), 0) AS total, coalesce(sum(collected), 0) AS collected
            FROM web_crawl_tasks
            WHERE %(city)s::text IS NULL OR city_slug = %(city)s
            GROUP BY city_slug, kind, status
            ORDER BY city_slug, kind, status
            """,
            {"city": city_slug},
        )
        return list(cur.fetchall())
