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
    schedule            jsonb,
    timezone            text,
    attributes          jsonb,
    flags               jsonb,
    raw                 jsonb NOT NULL,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    reviews_synced_at   timestamptz
);

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

-- Очередь обхода: тайлы карты по рубрике/запросу. Позволяет продолжить
-- прерванный обход и дробить области, где выдача упирается в лимит API.
CREATE TABLE IF NOT EXISTS crawl_tasks (
    id              bigserial PRIMARY KEY,
    region_id       bigint NOT NULL,
    rubric_id       bigint,
    query           text,
    min_lon         double precision NOT NULL,
    min_lat         double precision NOT NULL,
    max_lon         double precision NOT NULL,
    max_lat         double precision NOT NULL,
    depth           integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'done', 'split', 'error')),
    total           integer,
    fetched         integer,
    attempts        integer NOT NULL DEFAULT 0,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK (rubric_id IS NOT NULL OR query IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_crawl_tasks_tile ON crawl_tasks (
    region_id, coalesce(rubric_id, 0), coalesce(query, ''), min_lon, min_lat, max_lon, max_lat
);
CREATE INDEX IF NOT EXISTS idx_crawl_tasks_status ON crawl_tasks (status, depth, id);

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
