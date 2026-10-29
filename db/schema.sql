-- =====================================================================
-- V7.0 · PostgreSQL + PostGIS 建库草稿
-- ---------------------------------------------------------------------
-- 目标：把今天导出的 cities_master.json 直接灌进来，明天就能跑查询。
-- 运行：
--   createdb housing
--   psql housing -f schema.sql
--   psql housing -c "COPY cities FROM '/data/cities.csv' CSV HEADER;"
-- 说明：
--   · 空间字段用 geom（PostGIS），同时保留 lat/lng 方便前端
--   · market_snapshots 按月存真实房价/租金，将来爬虫直接写这张表
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS postgis;
-- gen_random_uuid() 是 PostgreSQL 13+ 内置函数，不再需要 uuid-ossp 扩展

-- ---------- 城市 ----------
CREATE TABLE IF NOT EXISTS cities (
    code            TEXT PRIMARY KEY,              -- 行政区划码
    name            TEXT NOT NULL,
    tier            SMALLINT NOT NULL CHECK (tier BETWEEN 1 AND 5),
    tier_label      TEXT,
    lat             DOUBLE PRECISION NOT NULL,
    lng             DOUBLE PRECISION NOT NULL,
    geom            GEOGRAPHY(POINT, 4326),
    price_sqm       NUMERIC(12, 2) NOT NULL,       -- 基准房价 元/㎡
    rent_sqm        NUMERIC(8, 2)  NOT NULL,       -- 基准租金 元/㎡·月
    avg_salary      NUMERIC(12, 2) NOT NULL,       -- 基准月薪
    source          TEXT,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cities_geom ON cities USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_cities_tier ON cities (tier);

-- ---------- 区县 / 区位 ----------
CREATE TABLE IF NOT EXISTS districts (
    id          BIGSERIAL PRIMARY KEY,
    city_code   TEXT NOT NULL REFERENCES cities(code) ON DELETE CASCADE,
    zone_key    TEXT,                              -- core / main / suburb / 行政区名
    name        TEXT NOT NULL,
    lat         DOUBLE PRECISION,
    lng         DOUBLE PRECISION,
    geom        GEOGRAPHY(POINT, 4326),
    price_sqm   NUMERIC(12, 2),
    rent_ref    NUMERIC(10, 2),
    commute_min NUMERIC(6, 1),
    UNIQUE (city_code, name)
);

CREATE INDEX IF NOT EXISTS idx_districts_city ON districts (city_code);


-- ---------- 房源（V7 推荐“最适合的 20 套房子”的数据底座） ----------
CREATE TABLE IF NOT EXISTS listings (
    id           BIGSERIAL PRIMARY KEY,
    city_code    TEXT NOT NULL REFERENCES cities(code) ON DELETE CASCADE,
    district_id  BIGINT REFERENCES districts(id) ON DELETE SET NULL,
    community    TEXT,                              -- 小区名
    title        TEXT,
    total_price  NUMERIC(14, 2) NOT NULL,           -- 总价（元）
    area_sqm     NUMERIC(8, 2) NOT NULL,            -- 建筑面积
    price_sqm    NUMERIC(12, 2) GENERATED ALWAYS AS
                 (CASE WHEN area_sqm > 0 THEN total_price / area_sqm ELSE NULL END) STORED,
    bedrooms     SMALLINT,
    lat          DOUBLE PRECISION,
    lng          DOUBLE PRECISION,
    geom         GEOGRAPHY(POINT, 4326),
    source       TEXT NOT NULL,
    url          TEXT,
    listed_at    DATE,
    raw          JSONB,
    created_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_listings_city_price ON listings (city_code, total_price);
CREATE INDEX IF NOT EXISTS idx_listings_city_area  ON listings (city_code, area_sqm);
CREATE INDEX IF NOT EXISTS idx_listings_geom       ON listings USING GIST (geom);

-- ---------- POI（学校/医院/地铁/商场，未来算“生活便利度”） ----------
CREATE TABLE IF NOT EXISTS pois (
    id         BIGSERIAL PRIMARY KEY,
    city_code  TEXT NOT NULL REFERENCES cities(code) ON DELETE CASCADE,
    category   TEXT NOT NULL,                       -- school / hospital / metro / mall
    name       TEXT NOT NULL,
    lat        DOUBLE PRECISION,
    lng        DOUBLE PRECISION,
    geom       GEOGRAPHY(POINT, 4326),
    source     TEXT,
    UNIQUE (city_code, category, name, lat, lng)
);

CREATE INDEX IF NOT EXISTS idx_pois_city_cat ON pois (city_code, category);
CREATE INDEX IF NOT EXISTS idx_pois_geom     ON pois USING GIST (geom);
-- ---------- 真实市场快照（爬虫/人工录入） ----------
CREATE TABLE IF NOT EXISTS market_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    city_code   TEXT NOT NULL REFERENCES cities(code) ON DELETE CASCADE,
    district_id BIGINT REFERENCES districts(id) ON DELETE SET NULL,
    asof        DATE NOT NULL,
    price_sqm   NUMERIC(12, 2),
    rent_sqm    NUMERIC(8, 2),
    source      TEXT NOT NULL,
    raw         JSONB,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (city_code, district_id, asof, source)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_city_asof ON market_snapshots (city_code, asof DESC);

-- ---------- 外部基准（统计局 / 中指研究院） ----------
CREATE TABLE IF NOT EXISTS benchmarks (
    id          BIGSERIAL PRIMARY KEY,
    metric      TEXT NOT NULL,                     -- new_home_price / rent_50city ...
    value       NUMERIC(14, 4) NOT NULL,
    unit        TEXT,
    asof        DATE,
    publisher   TEXT,
    source_url  TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (metric, asof, publisher)
);

-- ---------- 推演记录（可选，用于真实回测） ----------
CREATE TABLE IF NOT EXISTS simulation_runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ DEFAULT now(),
    city_code   TEXT REFERENCES cities(code) ON DELETE SET NULL,
    params      JSONB NOT NULL,
    summary     JSONB,
    app_version TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_city_created ON simulation_runs (city_code, created_at DESC);

-- ---------- 回测样本（真实回访） ----------
CREATE TABLE IF NOT EXISTS backtest_cases (
    id                 BIGSERIAL PRIMARY KEY,
    case_id            TEXT UNIQUE NOT NULL,
    city_code          TEXT REFERENCES cities(code) ON DELETE SET NULL,
    start_year         SMALLINT NOT NULL,
    income             NUMERIC(12, 2) NOT NULL,
    savings            NUMERIC(14, 2) NOT NULL,
    house_price        NUMERIC(14, 2) NOT NULL,
    actual_savings_5y  NUMERIC(14, 2),
    actual_bought      BOOLEAN,
    actual_ratio       NUMERIC(6, 4),
    source             TEXT,
    created_at         TIMESTAMPTZ DEFAULT now()
);

-- ---------- 一个视图：城市最新市场快照 ----------
CREATE OR REPLACE VIEW city_latest_market AS
SELECT DISTINCT ON (c.code)
       c.code, c.name, c.tier,
       s.asof, s.price_sqm, s.rent_sqm, s.source
FROM cities c
LEFT JOIN market_snapshots s ON s.city_code = c.code
ORDER BY c.code, s.asof DESC NULLS LAST;

-- ---------- 导入提示 ----------
-- 1) cities_master.json → CSV（用 export_city_master.py 生成后转 CSV）
--    jq -r '.cities[] | [.code,.name,.tier,.tier_label,.lat,.lng,.price_sqm,.rent_sqm,.avg_salary,.source] | @csv'
-- 2) COPY cities (code,name,tier,tier_label,lat,lng,price_sqm,rent_sqm,avg_salary,source)
--    FROM '/data/cities.csv' CSV;
-- 3) 别忘了回填 geom：
--    UPDATE cities SET geom = ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography WHERE geom IS NULL;


