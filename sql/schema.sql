-- Схема хранения данных 2ГИС (PostgreSQL).
-- Имена таблиц без префикса схемы: `python main.py init-db` создаёт схему
-- (по умолчанию `twogis`) и выполняет этот файл с search_path на неё.
-- Вручную: CREATE SCHEMA IF NOT EXISTS twogis; SET search_path TO twogis; \i sql/schema.sql

-- Регионы (города) 2ГИС и их границы
CREATE TABLE IF NOT EXISTS regions (
    id              bigint PRIMARY KEY,
    name            text NOT NULL,
    type            text,
    min_lon         double precision,
    min_lat         double precision,
    max_lon         double precision,
    max_lat         double precision,
    raw             jsonb NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Рубрикатор (группы и конечные рубрики)
CREATE TABLE IF NOT EXISTS rubrics (
    id              bigint PRIMARY KEY,
    parent_id       bigint,
    name            text NOT NULL,
    alias           text,
    type            text,                      -- group | rubric
    raw             jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Организации (юр. сущность, объединяющая филиалы)
CREATE TABLE IF NOT EXISTS organizations (
    id              bigint PRIMARY KEY,
    name            text,
    branch_count    integer,
    raw             jsonb,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Филиалы (конкретные точки на карте)
CREATE TABLE IF NOT EXISTS branches (
    id                  bigint PRIMARY KEY,
    raw_id              text NOT NULL,
    org_id              bigint REFERENCES organizations(id) ON DELETE SET NULL,
    region_id           bigint,
    name                text NOT NULL,
    name_primary        text,
    name_extension      text,
    legal_name          text,
    address_name        text,
    full_address_name   text,
    address_comment     text,
    postcode            text,
    building_id         bigint,
    city                text,
    district            text,
    lat                 double precision,
    lon                 double precision,
    rating              numeric(3, 2),
    review_count        integer,
    org_rating          numeric(3, 2),
    org_review_count    integer,
    primary_rubric      text,
    rubrics             text[],
    schedule            jsonb,
    timezone            text,
    attributes          jsonb,
    flags               jsonb,
    raw                 jsonb NOT NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    reviews_synced_at   timestamptz,
    card_synced_at      timestamptz
);

-- Миграция колонок категорий для уже существующих таблиц
ALTER TABLE branches ADD COLUMN IF NOT EXISTS primary_rubric text;
ALTER TABLE branches ADD COLUMN IF NOT EXISTS rubrics text[];
ALTER TABLE branches ADD COLUMN IF NOT EXISTS card_synced_at timestamptz;
ALTER TABLE branches ADD COLUMN IF NOT EXISTS card_attempts smallint NOT NULL DEFAULT 0;
ALTER TABLE branches ADD COLUMN IF NOT EXISTS reviews_attempts smallint NOT NULL DEFAULT 0;
COMMENT ON COLUMN branches.card_attempts IS 'Сколько раз не удалось получить полную карточку (после 3 объект больше не запрашивается)';
COMMENT ON COLUMN branches.reviews_attempts IS 'Сколько раз лента отзывов не собралась до конца (после 3 объект больше не запрашивается)';

-- Автоматическое заполнение категорий для ранее собранных записей
UPDATE branches b
SET primary_rubric = r.name
FROM branch_rubrics br
JOIN rubrics r ON r.id = br.rubric_id
WHERE br.branch_id = b.id AND br.is_primary AND (b.primary_rubric IS NULL OR b.primary_rubric = '');

UPDATE branches b
SET primary_rubric = r.name
FROM branch_rubrics br
JOIN rubrics r ON r.id = br.rubric_id
WHERE br.branch_id = b.id AND (b.primary_rubric IS NULL OR b.primary_rubric = '');

UPDATE branches b
SET rubrics = sub.r_names
FROM (
    SELECT br.branch_id, array_agg(r.name ORDER BY br.is_primary DESC) AS r_names
    FROM branch_rubrics br
    JOIN rubrics r ON r.id = br.rubric_id
    GROUP BY br.branch_id
) sub
WHERE sub.branch_id = b.id AND (b.rubrics IS NULL OR array_length(b.rubrics, 1) = 0);

CREATE INDEX IF NOT EXISTS idx_branches_primary_rubric ON branches (primary_rubric);
CREATE INDEX IF NOT EXISTS idx_branches_rubrics_gin ON branches USING gin (rubrics);

CREATE INDEX IF NOT EXISTS idx_branches_coords ON branches (lat, lon);
CREATE INDEX IF NOT EXISTS idx_branches_org ON branches (org_id);
CREATE INDEX IF NOT EXISTS idx_branches_region ON branches (region_id);
CREATE INDEX IF NOT EXISTS idx_branches_reviews_synced ON branches (reviews_synced_at NULLS FIRST);

-- Связь филиал <-> рубрика
CREATE TABLE IF NOT EXISTS branch_rubrics (
    branch_id       bigint NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    rubric_id       bigint NOT NULL REFERENCES rubrics(id) ON DELETE CASCADE,
    is_primary      boolean NOT NULL DEFAULT false,
    PRIMARY KEY (branch_id, rubric_id)
);

CREATE INDEX IF NOT EXISTS idx_branch_rubrics_rubric ON branch_rubrics (rubric_id);

-- Контакты филиала: телефоны, сайты, email, соцсети, мессенджеры
CREATE TABLE IF NOT EXISTS contacts (
    branch_id       bigint NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    type            text NOT NULL,
    value           text NOT NULL,
    text            text,
    url             text,
    comment         text,
    position        integer NOT NULL,
    PRIMARY KEY (branch_id, type, value)
);

CREATE INDEX IF NOT EXISTS idx_contacts_type ON contacts (type);

-- Отзывы
CREATE TABLE IF NOT EXISTS reviews (
    id                      text PRIMARY KEY,
    branch_id               bigint NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    provider                text,
    rating                  smallint,
    text                    text,
    user_id                 text,
    user_name               text,
    user_reviews_count      integer,
    likes_count             integer NOT NULL DEFAULT 0,
    comments_count          integer NOT NULL DEFAULT 0,
    photos_count            integer NOT NULL DEFAULT 0,
    is_verified             boolean,
    is_hidden               boolean,
    hiding_reason           text,
    official_answer_text    text,
    official_answer_date    timestamptz,
    date_created            timestamptz,
    date_edited             timestamptz,
    url                     text,
    raw                     jsonb NOT NULL,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reviews_branch_date ON reviews (branch_id, date_created DESC);

-- Комментарии к отзывам: официальные ответы организаций и реплики пользователей
CREATE TABLE IF NOT EXISTS review_comments (
    id                  text PRIMARY KEY,
    review_id           text NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    branch_id           bigint NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    text                text,
    is_official_answer  boolean,
    author_name         text,
    date_created        timestamptz,
    is_hidden           boolean,
    raw                 jsonb NOT NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_comments_review ON review_comments (review_id);

-- Очередь браузерного обхода сайта 2ГИС. Задача = поисковый запрос, рубрика
-- или здание. total/collected позволяют проверить полноту сбора, status —
-- продолжить прерванный обход с места остановки.
CREATE TABLE IF NOT EXISTS web_crawl_tasks (
    id              bigserial PRIMARY KEY,
    city_slug       text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('query', 'rubric', 'building', 'area', 'transport')),
    key             text NOT NULL,
    label           text,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'done', 'incomplete', 'error')),
    total           integer,
    collected       integer,
    pages_total     integer,
    pages_done      integer,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (city_slug, kind, key)
);

CREATE INDEX IF NOT EXISTS idx_web_crawl_tasks_queue ON web_crawl_tasks (city_slug, status, kind, id);
ALTER TABLE web_crawl_tasks DROP CONSTRAINT IF EXISTS web_crawl_tasks_kind_check;
ALTER TABLE web_crawl_tasks ADD CONSTRAINT web_crawl_tasks_kind_check
    CHECK (kind IN ('query', 'rubric', 'building', 'area', 'transport'));

-- Задачи зданий раньше могли ставиться с составным ключом '<id>_<хэш>': на такой
-- странице ответ «В здании» не совпадал по building_id. Ключ приводится к числовому ID.
DELETE FROM web_crawl_tasks t
USING web_crawl_tasks x
WHERE t.kind = 'building' AND t.key LIKE '%\_%'
  AND x.city_slug = t.city_slug AND x.kind = 'building'
  AND split_part(x.key, '_', 1) = split_part(t.key, '_', 1)
  AND (x.key NOT LIKE '%\_%' OR x.id < t.id);
UPDATE web_crawl_tasks
SET key = split_part(key, '_', 1), attempts = 0,
    status = CASE WHEN status IN ('error', 'incomplete') THEN 'pending' ELSE status END
WHERE kind = 'building' AND key LIKE '%\_%';

-- Плоское представление филиалов для выгрузок и аналитики
CREATE OR REPLACE VIEW v_branches AS
SELECT
    b.id,
    b.name,
    b.legal_name,
    o.name AS org_name,
    b.city,
    b.district,
    b.full_address_name,
    b.address_comment,
    b.lat,
    b.lon,
    b.rating,
    b.review_count,
    (SELECT r.name FROM branch_rubrics br JOIN rubrics r ON r.id = br.rubric_id
      WHERE br.branch_id = b.id AND br.is_primary LIMIT 1) AS primary_rubric,
    (SELECT array_agg(r.name ORDER BY br.is_primary DESC, r.name) FROM branch_rubrics br
      JOIN rubrics r ON r.id = br.rubric_id WHERE br.branch_id = b.id) AS rubrics,
    (SELECT array_agg(c.text ORDER BY c.position) FROM contacts c
      WHERE c.branch_id = b.id AND c.type = 'phone') AS phones,
    (SELECT array_agg(coalesce(c.text, c.url, c.value) ORDER BY c.position) FROM contacts c
      WHERE c.branch_id = b.id AND c.type = 'website') AS websites,
    (SELECT array_agg(c.value ORDER BY c.position) FROM contacts c
      WHERE c.branch_id = b.id AND c.type = 'email') AS emails,
    (SELECT count(*) FROM reviews rv WHERE rv.branch_id = b.id) AS reviews_collected,
    b.updated_at
FROM branches b
LEFT JOIN organizations o ON o.id = b.org_id;

-- ============================================================================
-- Общественный транспорт (автобусы, метро, троллейбусы, трамваи, маршрутки)
-- ============================================================================

-- Остановки и станции
CREATE TABLE IF NOT EXISTS transport_stops (
    id TEXT PRIMARY KEY,
    region_id BIGINT REFERENCES regions (id) ON DELETE SET NULL,
    region TEXT,
    city TEXT,
    city_slug TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'station',
    subtype TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    district TEXT,
    raw JSONB,
    scraped_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transport_stops_city ON transport_stops (city_slug);
CREATE INDEX IF NOT EXISTS idx_transport_stops_coords ON transport_stops (lat, lon);
CREATE INDEX IF NOT EXISTS idx_transport_stops_subtype ON transport_stops (subtype);

-- Маршруты транспорта
CREATE TABLE IF NOT EXISTS transport_routes (
    id TEXT NOT NULL,
    city_slug TEXT NOT NULL,
    region_id BIGINT REFERENCES regions (id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    subtype TEXT NOT NULL,
    from_name TEXT,
    to_name TEXT,
    color TEXT,
    raw JSONB,
    scraped_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    PRIMARY KEY (city_slug, id)
);

CREATE INDEX IF NOT EXISTS idx_transport_routes_city_subtype ON transport_routes (city_slug, subtype);

-- Связка: маршрут x остановка (соответствует формату dgis_stations_parser)
CREATE TABLE IF NOT EXISTS transport_route_stops (
    id BIGSERIAL PRIMARY KEY,
    region TEXT,
    city TEXT,
    city_slug TEXT NOT NULL,
    route_id TEXT NOT NULL,
    route_number TEXT,
    route_subtype TEXT,
    route_from TEXT,
    route_to TEXT,
    stop_id TEXT NOT NULL,
    stop_name TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    district TEXT,
    color TEXT,
    scraped_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    UNIQUE (city_slug, route_id, stop_id)
);

CREATE INDEX IF NOT EXISTS idx_transport_route_stops_route ON transport_route_stops (city_slug, route_id);
CREATE INDEX IF NOT EXISTS idx_transport_route_stops_stop ON transport_route_stops (stop_id);
CREATE INDEX IF NOT EXISTS idx_transport_route_stops_subtype ON transport_route_stops (route_subtype);

-- Остановки маршрута по порядку, отдельно для каждого направления (со страницы /route/{id})
CREATE TABLE IF NOT EXISTS transport_route_platforms (
    city_slug       text NOT NULL,
    route_id        text NOT NULL,
    direction_no    smallint NOT NULL,
    direction_type  text,
    seq             smallint NOT NULL,
    platform_id     text,
    stop_id         text,
    stop_name       text,
    lat             double precision,
    lon             double precision,
    scraped_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (city_slug, route_id, direction_no, seq)
);

CREATE INDEX IF NOT EXISTS idx_transport_route_platforms_stop ON transport_route_platforms (stop_id);

-- Когда обработана страница маршрута (в т.ч. если остановок на ней не оказалось)
ALTER TABLE transport_routes ADD COLUMN IF NOT EXISTS platforms_synced_at timestamptz;
UPDATE transport_routes r SET platforms_synced_at = now()
WHERE platforms_synced_at IS NULL AND EXISTS (
    SELECT 1 FROM transport_route_platforms p WHERE p.city_slug = r.city_slug AND p.route_id = r.id);
COMMENT ON COLUMN transport_routes.platforms_synced_at IS 'Когда собрана страница маршрута с остановками по порядку (NULL — ещё не собрана)';

-- ID маршрутов приводятся к числовому виду (раньше попадались составные '<id>_<хэш>')
DELETE FROM transport_routes r
WHERE r.id LIKE '%\_%' AND EXISTS (
    SELECT 1 FROM transport_routes x WHERE x.city_slug = r.city_slug AND x.id = split_part(r.id, '_', 1));
UPDATE transport_routes SET id = split_part(id, '_', 1) WHERE id LIKE '%\_%';

-- То же для остановок и связок маршрут × остановка: данные составной записи переносятся
-- на числовой ID, составные записи удаляются (повторный запуск ничего не меняет)
UPDATE transport_stops p
SET raw = c.raw, district = coalesce(c.district, p.district), subtype = coalesce(c.subtype, p.subtype),
    region = coalesce(c.region, p.region), city = coalesce(c.city, p.city)
FROM transport_stops c
WHERE c.id LIKE '%\_%' AND p.id = split_part(c.id, '_', 1) AND p.raw IS NULL AND c.raw IS NOT NULL;
INSERT INTO transport_stops (id, region_id, region, city, city_slug, name, type, subtype, lat, lon, district, raw, scraped_at)
SELECT DISTINCT ON (split_part(id, '_', 1))
       split_part(id, '_', 1), region_id, region, city, city_slug, name, type, subtype, lat, lon, district, raw, scraped_at
FROM transport_stops WHERE id LIKE '%\_%'
ORDER BY split_part(id, '_', 1), scraped_at DESC
ON CONFLICT (id) DO NOTHING;
DELETE FROM transport_stops WHERE id LIKE '%\_%';

DELETE FROM transport_route_stops r
USING transport_route_stops x
WHERE (r.stop_id LIKE '%\_%' OR r.route_id LIKE '%\_%')
  AND x.city_slug = r.city_slug
  AND split_part(x.route_id, '_', 1) = split_part(r.route_id, '_', 1)
  AND split_part(x.stop_id, '_', 1) = split_part(r.stop_id, '_', 1)
  AND (x.stop_id NOT LIKE '%\_%' AND x.route_id NOT LIKE '%\_%' OR x.id < r.id);
UPDATE transport_route_stops
SET stop_id = split_part(stop_id, '_', 1), route_id = split_part(route_id, '_', 1)
WHERE stop_id LIKE '%\_%' OR route_id LIKE '%\_%';

-- Представление для совместимости с кодом, ожидающим таблицу 2gis_bus_stations
CREATE OR REPLACE VIEW v_2gis_bus_stations AS
SELECT
    id,
    region,
    city,
    city_slug,
    route_id,
    route_number,
    route_subtype,
    route_from,
    route_to,
    stop_id,
    stop_name,
    lat,
    lon,
    district,
    scraped_at
FROM transport_route_stops;

-- ============================================================================
-- Где находится объект: уровни адреса из adm_div карточки 2ГИС
-- ============================================================================
CREATE OR REPLACE FUNCTION adm_name(adm jsonb, level text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT replace(a->>'name', chr(160), ' ')
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(adm) = 'array' THEN adm ELSE '[]'::jsonb END) a
    WHERE a->>'type' = level
    LIMIT 1
$$;

-- ============================================================================
-- Единое представление всех объектов (организации + транспорт) с категориями
-- ============================================================================
CREATE OR REPLACE VIEW v_all_objects AS
SELECT
    coalesce(b.raw->>'type', 'branch') AS object_type,
    b.id::text AS id,
    b.name,
    coalesce(b.primary_rubric, 'Без категории') AS category,
    b.rubrics AS subcategories,
    b.city,
    b.district,
    coalesce(b.full_address_name, b.address_name) AS address,
    b.lat,
    b.lon,
    b.rating,
    b.review_count,
    b.schedule,
    b.first_seen_at AS created_at,
    adm_name(b.raw->'adm_div', 'region') AS region,
    adm_name(b.raw->'adm_div', 'district_area') AS district_area,
    coalesce(adm_name(b.raw->'adm_div', 'city'), adm_name(b.raw->'adm_div', 'settlement')) AS locality,
    adm_name(b.raw->'adm_div', 'living_area') AS microdistrict
FROM branches b
UNION ALL
SELECT
    'transport_stop' AS object_type,
    s.id AS id,
    s.name,
    'Общественный транспорт' AS category,
    ARRAY[coalesce(s.subtype, 'остановка')] AS subcategories,
    s.city,
    coalesce(adm_name(s.raw->'adm_div', 'district'), s.district) AS district,
    NULL AS address,
    s.lat,
    s.lon,
    NULL AS rating,
    NULL AS review_count,
    NULL AS schedule,
    s.scraped_at AS created_at,
    adm_name(s.raw->'adm_div', 'region') AS region,
    adm_name(s.raw->'adm_div', 'district_area') AS district_area,
    coalesce(adm_name(s.raw->'adm_div', 'city'), adm_name(s.raw->'adm_div', 'settlement')) AS locality,
    adm_name(s.raw->'adm_div', 'living_area') AS microdistrict
FROM transport_stops s;

-- Остановки с полным адресным положением
CREATE OR REPLACE VIEW v_transport_stops AS
SELECT
    s.id, s.name, s.subtype, s.lat, s.lon, s.city_slug,
    adm_name(s.raw->'adm_div', 'region') AS region,
    adm_name(s.raw->'adm_div', 'district_area') AS district_area,
    coalesce(adm_name(s.raw->'adm_div', 'city'), adm_name(s.raw->'adm_div', 'settlement')) AS locality,
    coalesce(adm_name(s.raw->'adm_div', 'district'), s.district) AS district,
    adm_name(s.raw->'adm_div', 'living_area') AS microdistrict
FROM transport_stops s;

-- Маршрут -> остановки по порядку -> где каждая находится
CREATE OR REPLACE VIEW v_route_stops_ordered AS
SELECT
    r.city_slug, r.subtype AS route_type, r.name AS route_number,
    r.from_name AS route_from, r.to_name AS route_to, r.id AS route_id,
    p.direction_no, p.direction_type, p.seq,
    p.stop_id, p.stop_name, p.lat, p.lon,
    s.region, s.district_area, s.locality, s.district, s.microdistrict
FROM transport_route_platforms p
JOIN transport_routes r ON r.city_slug = p.city_slug AND r.id = p.route_id
LEFT JOIN v_transport_stops s ON s.id = p.stop_id;

-- Маршрут одной строкой на направление: число остановок и нумерованный список остановок
CREATE OR REPLACE VIEW v_routes AS
SELECT
    city_slug, route_type, route_number, route_from, route_to, route_id,
    direction_no, direction_type,
    count(*) AS stops_count,
    jsonb_agg(jsonb_build_object(
        'n', seq, 'stop', stop_name, 'stop_id', stop_id,
        'locality', locality, 'district', district, 'microdistrict', microdistrict,
        'lat', lat, 'lon', lon) ORDER BY seq) AS stops,
    string_agg(seq || '. ' || coalesce(stop_name, '?'), ', ' ORDER BY seq) AS stops_text,
    array_agg(DISTINCT locality) FILTER (WHERE locality IS NOT NULL) AS localities
FROM v_route_stops_ordered
GROUP BY city_slug, route_type, route_number, route_from, route_to, route_id, direction_no, direction_type;

-- ============================================================================
-- Описания (комментарии) таблиц и колонок на русском языке
-- ============================================================================

-- 1. regions (Регионы и города)
COMMENT ON TABLE regions IS 'Регионы и города 2ГИС с их географическими границами (bbox) и метаданными';
COMMENT ON COLUMN regions.id IS 'Уникальный числовой ID региона/города в 2ГИС';
COMMENT ON COLUMN regions.name IS 'Название региона или города (например: Астана, Алматы)';
COMMENT ON COLUMN regions.type IS 'Тип географической сущности (city / region)';
COMMENT ON COLUMN regions.min_lon IS 'Минимальная географическая долгота границы (WGS84)';
COMMENT ON COLUMN regions.min_lat IS 'Минимальная географическая широта границы (WGS84)';
COMMENT ON COLUMN regions.max_lon IS 'Максимальная географическая долгота границы (WGS84)';
COMMENT ON COLUMN regions.max_lat IS 'Максимальная географическая широта границы (WGS84)';
COMMENT ON COLUMN regions.raw IS 'Полный исходный ответ 2ГИС в формате JSONB';
COMMENT ON COLUMN regions.updated_at IS 'Дата и время последнего обновления записи в БД';

-- 2. organizations (Организации и сети)
COMMENT ON TABLE organizations IS 'Организации (юридические лица, управляющие компании и сетевые бренды, объединяющие филиалы)';
COMMENT ON COLUMN organizations.id IS 'Уникальный числовой ID организации/сети в 2ГИС';
COMMENT ON COLUMN organizations.name IS 'Название компании или бренда (например: Kaspi Bank, Dodo Pizza)';
COMMENT ON COLUMN organizations.branch_count IS 'Количество филиалов у данной организации';
COMMENT ON COLUMN organizations.raw IS 'Полные исходные данные организации в формате JSONB';
COMMENT ON COLUMN organizations.updated_at IS 'Дата и время последнего обновления';

-- 3. rubrics (Справочник категорий)
COMMENT ON TABLE rubrics IS 'Рубрикатор категорий 2ГИС: разделы, метарубрики и конкретные виды деятельности';
COMMENT ON COLUMN rubrics.id IS 'Уникальный ID рубрики/категории в 2ГИС';
COMMENT ON COLUMN rubrics.parent_id IS 'ID родительской категории/раздела (NULL для корневых разделов)';
COMMENT ON COLUMN rubrics.name IS 'Название категории (например: Жилые комплексы, Кафе, Аптеки, Парки)';
COMMENT ON COLUMN rubrics.alias IS 'Короткий URL-алиас рубрики на сайте 2gis.kz';
COMMENT ON COLUMN rubrics.type IS 'Тип рубрики: metarubric (главный раздел), rubric (категория), group (группа)';
COMMENT ON COLUMN rubrics.raw IS 'Исходные метаданные рубрики в формате JSONB';
COMMENT ON COLUMN rubrics.updated_at IS 'Дата последнего обновления';

-- 4. branches (Все объекты города)
COMMENT ON TABLE branches IS 'Все объекты города: карточки организаций, филиалы, жилые комплексы (ЖК), здания, парки, скверы, детские площадки, достопримечательности';
COMMENT ON COLUMN branches.id IS 'Уникальный числовой ID карточки объекта в 2ГИС';
COMMENT ON COLUMN branches.raw_id IS 'Исходный идентификатор карточки из 2ГИС (включая строковые и составные хеши)';
COMMENT ON COLUMN branches.org_id IS 'Ссылка на головную организацию/сеть (organizations.id)';
COMMENT ON COLUMN branches.region_id IS 'Ссылка на регион присутствия объекта (regions.id)';
COMMENT ON COLUMN branches.name IS 'Полное название объекта, ЖК, парка или заведения';
COMMENT ON COLUMN branches.name_primary IS 'Основная часть названия (бренд/наименование)';
COMMENT ON COLUMN branches.name_extension IS 'Уточняющая часть названия (например: жилой комплекс, кафе, салон красоты)';
COMMENT ON COLUMN branches.legal_name IS 'Официальное юридическое наименование (ТОО, АО, ИП)';
COMMENT ON COLUMN branches.address_name IS 'Название улицы и номер строения/дома';
COMMENT ON COLUMN branches.full_address_name IS 'Полный адрес объекта с городом, районом и улицей';
COMMENT ON COLUMN branches.address_comment IS 'Уточнение к адресу (этаж, подъезд, блок, ориентир)';
COMMENT ON COLUMN branches.postcode IS 'Почтовый индекс';
COMMENT ON COLUMN branches.building_id IS 'Уникальный ID здания в картографической базе 2ГИС';
COMMENT ON COLUMN branches.city IS 'Город или населенный пункт';
COMMENT ON COLUMN branches.district IS 'Административный район города (например: Есиль район, Нура район)';
COMMENT ON COLUMN branches.lat IS 'Географическая широта объекта (WGS84)';
COMMENT ON COLUMN branches.lon IS 'Географическая долгота объекта (WGS84)';
COMMENT ON COLUMN branches.rating IS 'Пользовательский рейтинг объекта по 5-балльной шкале (например: 4.80)';
COMMENT ON COLUMN branches.review_count IS 'Общее количество отзывов пользователей к объекту';
COMMENT ON COLUMN branches.org_rating IS 'Средний рейтинг головной организации по всем её точкам';
COMMENT ON COLUMN branches.org_review_count IS 'Суммарное количество отзывов по всей сети';
COMMENT ON COLUMN branches.primary_rubric IS 'Основная категория объекта (например: Жилые комплексы, Кафе, Парки, Аптеки)';
COMMENT ON COLUMN branches.rubrics IS 'Массив всех категорий и видов деятельности объекта (text[])';
COMMENT ON COLUMN branches.schedule IS 'Подробный график работы по дням недели (пн-вс, время, обед, признак 24/7) в JSONB';
COMMENT ON COLUMN branches.timezone IS 'Часовой пояс объекта';
COMMENT ON COLUMN branches.attributes IS 'Характеристики и удобства объекта (парковка, Wi-Fi, способы оплаты) в JSONB';
COMMENT ON COLUMN branches.flags IS 'Флаги объекта (наличие фото, доставка, запись онлайн)';
COMMENT ON COLUMN branches.raw IS 'Полный неизмененный JSON-документ карточки из 2ГИС';
COMMENT ON COLUMN branches.first_seen_at IS 'Дата и время первого добавления объекта в базу данных';
COMMENT ON COLUMN branches.updated_at IS 'Дата и время последнего обновления данных об объекте';
COMMENT ON COLUMN branches.reviews_synced_at IS 'Дата и время последней синхронизации отзывов к этому объекту';
COMMENT ON COLUMN branches.card_synced_at IS 'Когда собрана полная карточка (страница объекта: телефоны, соцсети, email, атрибуты). NULL — есть только данные из поисковой выдачи';

-- 5. branch_rubrics (Связка объектов и рубрик)
COMMENT ON TABLE branch_rubrics IS 'Связка многие-ко-многим между объектами и категориями рубрикатора';
COMMENT ON COLUMN branch_rubrics.branch_id IS 'ID объекта (branches.id)';
COMMENT ON COLUMN branch_rubrics.rubric_id IS 'ID рубрики (rubrics.id)';
COMMENT ON COLUMN branch_rubrics.is_primary IS 'Флаг: является ли данная категория основной для объекта';

-- 6. contacts (Контакты объектов)
COMMENT ON TABLE contacts IS 'Контактные данные объектов: телефоны, сайты, email, соцсети (Instagram, VK), мессенджеры (WhatsApp, Telegram)';
COMMENT ON COLUMN contacts.branch_id IS 'ID объекта, к которому относится контакт (branches.id)';
COMMENT ON COLUMN contacts.type IS 'Тип контакта (phone, website, email, instagram, whatsapp, telegram, vk)';
COMMENT ON COLUMN contacts.value IS 'Нормализованное значение (номер телефона, адрес сайта, email)';
COMMENT ON COLUMN contacts.text IS 'Отображаемый форматированный текст (например: +7 (7172) 12-34-56)';
COMMENT ON COLUMN contacts.url IS 'Прямая ссылка для перехода';
COMMENT ON COLUMN contacts.comment IS 'Примечание к контакту (например: отдел продаж, регистратура, доставка)';
COMMENT ON COLUMN contacts.position IS 'Порядковый номер контакта в карточке объекта';

-- 7. reviews (Отзывы)
COMMENT ON TABLE reviews IS 'Отзывы пользователей 2ГИС к объектам, ЖК, компаниям и заведениям';
COMMENT ON COLUMN reviews.id IS 'Уникальный строковый ID отзыва в 2ГИС';
COMMENT ON COLUMN reviews.branch_id IS 'ID объекта/филиала, к которому оставлен отзыв (branches.id)';
COMMENT ON COLUMN reviews.provider IS 'Источник отзыва (2gis, flamp)';
COMMENT ON COLUMN reviews.rating IS 'Оценка пользователя от 1 до 5 звезд';
COMMENT ON COLUMN reviews.text IS 'Текст отзыва пользователя';
COMMENT ON COLUMN reviews.user_id IS 'ID профиля автора отзыва';
COMMENT ON COLUMN reviews.user_name IS 'Имя и фамилия автора отзыва';
COMMENT ON COLUMN reviews.user_reviews_count IS 'Общее число отзывов, написанных данным автором';
COMMENT ON COLUMN reviews.likes_count IS 'Количество лайков (полезностей), полученных отзывом';
COMMENT ON COLUMN reviews.comments_count IS 'Количество комментариев к отзыву';
COMMENT ON COLUMN reviews.photos_count IS 'Количество прикрепленных фотографий к отзыву';
COMMENT ON COLUMN reviews.is_verified IS 'Подтвержден ли отзыв реальным визитом/чеком';
COMMENT ON COLUMN reviews.is_hidden IS 'Скрыт ли отзыв модерацией';
COMMENT ON COLUMN reviews.hiding_reason IS 'Причина скрытия отзыва при наличии';
COMMENT ON COLUMN reviews.official_answer_text IS 'Текст официального ответа от представителя организации';
COMMENT ON COLUMN reviews.official_answer_date IS 'Дата и время официального ответа';
COMMENT ON COLUMN reviews.date_created IS 'Дата и время публикации отзыва';
COMMENT ON COLUMN reviews.date_edited IS 'Дата и время последнего редактирования отзыва';
COMMENT ON COLUMN reviews.url IS 'Прямая ссылка на отзыв на сайте 2ГИС';
COMMENT ON COLUMN reviews.raw IS 'Полные исходные данные отзыва в формате JSONB';

-- 8. transport_stops (Остановки и станции)
COMMENT ON TABLE transport_stops IS 'Остановки и станции общественного транспорта: автобусы, метро, троллейбусы, трамваи, маршрутки';
COMMENT ON COLUMN transport_stops.id IS 'Уникальный ID остановки/станции в 2ГИС';
COMMENT ON COLUMN transport_stops.region_id IS 'ID региона';
COMMENT ON COLUMN transport_stops.region IS 'Название региона';
COMMENT ON COLUMN transport_stops.city IS 'Название города (например: Астана)';
COMMENT ON COLUMN transport_stops.city_slug IS 'Slug города на сайте 2gis.kz (например: astana)';
COMMENT ON COLUMN transport_stops.name IS 'Название остановки или станции (например: Дом Министерств)';
COMMENT ON COLUMN transport_stops.type IS 'Тип объекта (station)';
COMMENT ON COLUMN transport_stops.subtype IS 'Подтип транспорта (bus, metro, trolleybus, tram, shuttle_bus, stop)';
COMMENT ON COLUMN transport_stops.lat IS 'Географическая широта остановки (WGS84)';
COMMENT ON COLUMN transport_stops.lon IS 'Географическая долгота остановки (WGS84)';
COMMENT ON COLUMN transport_stops.district IS 'Административный район города';
COMMENT ON COLUMN transport_stops.raw IS 'Полный исходный ответ JSONB';
COMMENT ON COLUMN transport_stops.scraped_at IS 'Дата и время сбора данных об остановке';

-- 9. transport_routes (Маршруты транспорта)
COMMENT ON TABLE transport_routes IS 'Маршруты общественного транспорта: автобусные маршруты, линии метро, троллейбусы, трамваи';
COMMENT ON COLUMN transport_routes.id IS 'Уникальный ID маршрута в 2ГИС';
COMMENT ON COLUMN transport_routes.city_slug IS 'Slug города (astana, almaty)';
COMMENT ON COLUMN transport_routes.region_id IS 'ID региона';
COMMENT ON COLUMN transport_routes.name IS 'Номер маршрута (например: 10, 12, 100) или название линии метро';
COMMENT ON COLUMN transport_routes.subtype IS 'Тип транспорта (bus, metro, trolleybus, tram, shuttle_bus)';
COMMENT ON COLUMN transport_routes.from_name IS 'Начальная станция / конечная отправления';
COMMENT ON COLUMN transport_routes.to_name IS 'Конечная станция прибытия';
COMMENT ON COLUMN transport_routes.color IS 'Фирменный цвет маршрута или линии метро (HEX, например #E90101)';
COMMENT ON COLUMN transport_routes.raw IS 'Исходные данные маршрута в формате JSONB';
COMMENT ON COLUMN transport_routes.scraped_at IS 'Дата сбора данных';

-- 10. transport_route_stops (Связка маршрут x остановка)
COMMENT ON TABLE transport_route_stops IS 'Маршрутная схема: связка маршрут x остановка (полный маршрутный граф города, аналог 2gis_bus_stations)';
COMMENT ON COLUMN transport_route_stops.id IS 'Уникальный суррогатный первичный ключ';
COMMENT ON COLUMN transport_route_stops.region IS 'Название региона';
COMMENT ON COLUMN transport_route_stops.city IS 'Название города';
COMMENT ON COLUMN transport_route_stops.city_slug IS 'Slug города';
COMMENT ON COLUMN transport_route_stops.route_id IS 'ID маршрута в 2ГИС';
COMMENT ON COLUMN transport_route_stops.route_number IS 'Номер автобуса/маршрута или название линии';
COMMENT ON COLUMN transport_route_stops.route_subtype IS 'Тип транспорта (bus, metro, trolleybus, tram)';
COMMENT ON COLUMN transport_route_stops.route_from IS 'Начальная остановка маршрута';
COMMENT ON COLUMN transport_route_stops.route_to IS 'Конечная остановка маршрута';
COMMENT ON COLUMN transport_route_stops.stop_id IS 'ID остановки в 2ГИС';
COMMENT ON COLUMN transport_route_stops.stop_name IS 'Название остановки';
COMMENT ON COLUMN transport_route_stops.lat IS 'Географическая широта остановки';
COMMENT ON COLUMN transport_route_stops.lon IS 'Географическая долгота остановки';
COMMENT ON COLUMN transport_route_stops.district IS 'Административный район';
COMMENT ON COLUMN transport_route_stops.color IS 'Цвет линии/маршрута';
COMMENT ON COLUMN transport_route_stops.scraped_at IS 'Дата сбора';

-- transport_route_platforms (Остановки маршрута по порядку)
COMMENT ON TABLE transport_route_platforms IS 'Остановки каждого маршрута в порядке следования, отдельно по направлениям (прямое/обратное)';
COMMENT ON COLUMN transport_route_platforms.city_slug IS 'Slug города';
COMMENT ON COLUMN transport_route_platforms.route_id IS 'ID маршрута (transport_routes.id)';
COMMENT ON COLUMN transport_route_platforms.direction_no IS 'Номер направления: 0, 1 (обычно прямое и обратное)';
COMMENT ON COLUMN transport_route_platforms.direction_type IS 'Тип направления из 2ГИС: forward, backward, circular';
COMMENT ON COLUMN transport_route_platforms.seq IS 'Порядковый номер остановки в направлении, с 1';
COMMENT ON COLUMN transport_route_platforms.platform_id IS 'ID платформы (конкретная сторона остановки)';
COMMENT ON COLUMN transport_route_platforms.stop_id IS 'ID остановки (transport_stops.id)';
COMMENT ON COLUMN transport_route_platforms.stop_name IS 'Название остановки';
COMMENT ON COLUMN transport_route_platforms.lat IS 'Широта платформы';
COMMENT ON COLUMN transport_route_platforms.lon IS 'Долгота платформы';
COMMENT ON COLUMN transport_route_platforms.scraped_at IS 'Дата сбора';

-- 11. Представления (Views)
COMMENT ON VIEW v_all_objects IS 'Единый реестр ВСЕХ объектов города: организации, ЖК, парки, скверы, детские площадки, достопримечательности и остановки транспорта с категориями';
COMMENT ON VIEW v_branches IS 'Плоская аналитическая витрина карточек организаций с контактами, рубриками и количеством отзывов';
COMMENT ON VIEW v_2gis_bus_stations IS 'Представление для обратной совместимости со старыми запросами к таблице 2gis_bus_stations';
COMMENT ON VIEW v_transport_stops IS 'Остановки с адресным положением: область/город (region), район области, населённый пункт (locality), район города, микрорайон';
COMMENT ON VIEW v_route_stops_ordered IS 'Маршрут -> остановки по порядку в каждом направлении -> населённый пункт, район и микрорайон каждой остановки';
COMMENT ON VIEW v_routes IS 'Маршрут одной строкой на направление: тип, номер, откуда-куда, число остановок, остановки по порядку (stops — JSON с номером, названием, населённым пунктом, районом, координатами; stops_text — «1. …, 2. …»), населённые пункты на маршруте';
COMMENT ON FUNCTION adm_name(jsonb, text) IS 'Название уровня адреса (region, district_area, city, settlement, district, living_area) из adm_div карточки 2ГИС';

-- 12. web_crawl_tasks (Очередь и журнал полноты обхода)
COMMENT ON TABLE web_crawl_tasks IS 'Очередь браузерного обхода 2ГИС и журнал полноты: по каждому запросу/рубрике/зданию — сколько объектов заявил 2ГИС и сколько собрано';
COMMENT ON COLUMN web_crawl_tasks.id IS 'Уникальный ID задачи';
COMMENT ON COLUMN web_crawl_tasks.city_slug IS 'Slug города на 2gis.kz (например: astana)';
COMMENT ON COLUMN web_crawl_tasks.kind IS 'Тип задачи: query (поисковый запрос), rubric (рубрика 2ГИС), building (здание/ЖК — вкладка «В здании»), area (район: total — зданий по данным 2ГИС, collected — найдено зданий)';
COMMENT ON COLUMN web_crawl_tasks.key IS 'Ключ задачи: текст запроса, ID рубрики или ID здания';
COMMENT ON COLUMN web_crawl_tasks.label IS 'Человекочитаемое название (имя рубрики, название здания)';
COMMENT ON COLUMN web_crawl_tasks.status IS 'pending — в очереди, running — выполняется, done — собрано полностью, incomplete — собрано не всё (будет повтор), error — ошибка';
COMMENT ON COLUMN web_crawl_tasks.total IS 'Сколько объектов 2ГИС заявил в выдаче (total)';
COMMENT ON COLUMN web_crawl_tasks.collected IS 'Сколько уникальных объектов реально собрано';
COMMENT ON COLUMN web_crawl_tasks.pages_total IS 'Сколько страниц выдачи заявил 2ГИС';
COMMENT ON COLUMN web_crawl_tasks.pages_done IS 'Сколько страниц реально пройдено кликами';
COMMENT ON COLUMN web_crawl_tasks.attempts IS 'Число попыток выполнения задачи';
COMMENT ON COLUMN web_crawl_tasks.last_error IS 'Текст последней ошибки';
COMMENT ON COLUMN web_crawl_tasks.created_at IS 'Когда задача появилась в очереди';
COMMENT ON COLUMN web_crawl_tasks.updated_at IS 'Когда задача последний раз менялась';

-- review_comments (Комментарии к отзывам)
COMMENT ON TABLE review_comments IS 'Комментарии к отзывам: официальные ответы организаций и реплики пользователей';
COMMENT ON COLUMN review_comments.id IS 'ID комментария в 2ГИС';
COMMENT ON COLUMN review_comments.review_id IS 'ID отзыва (reviews.id)';
COMMENT ON COLUMN review_comments.branch_id IS 'ID объекта (branches.id)';
COMMENT ON COLUMN review_comments.text IS 'Текст комментария';
COMMENT ON COLUMN review_comments.is_official_answer IS 'Официальный ответ организации (true) или комментарий пользователя (false)';
COMMENT ON COLUMN review_comments.author_name IS 'Автор: имя пользователя или название организации';
COMMENT ON COLUMN review_comments.date_created IS 'Дата и время комментария';
COMMENT ON COLUMN review_comments.is_hidden IS 'Скрыт модерацией';
COMMENT ON COLUMN review_comments.raw IS 'Исходные данные комментария в JSONB';
COMMENT ON COLUMN review_comments.first_seen_at IS 'Когда комментарий впервые сохранён';
