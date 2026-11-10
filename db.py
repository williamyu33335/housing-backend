"""
V7.0 数据访问层：PostgreSQL + PostGIS
--------------------------------------------------------------------------
· 设置 DATABASE_URL 即启用；未设置时所有接口自动回退到内存常量
· 只做只读查询 + 推演留痕，业务计算仍在 main.py 的引擎里
"""
from __future__ import annotations

import json
import logging
import os

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # pragma: no cover
    psycopg2 = None
    RealDictCursor = None

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


def enabled() -> bool:
    return bool(DATABASE_URL and psycopg2)


def _connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=3)


def _query(sql: str, params=None) -> list[dict]:
    conn = _connect()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params or ())
                if cur.description is None:   # INSERT/UPDATE/DELETE 没有结果集
                    return []
                return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_cities() -> list[dict]:
    return _query("""
        SELECT code, name, tier, tier_label, lat, lng,
               price_sqm::float AS price, rent_sqm::float AS rent,
               avg_salary::float AS salary
        FROM cities
        ORDER BY tier, code
    """)


def list_districts(city_code: str) -> list[dict]:
    return _query("""
        SELECT id, city_code, COALESCE(zone_key, name) AS zone_key, name,
               lat, lng,
               COALESCE(price_sqm, 0)::float AS price_sqm,
               COALESCE(rent_ref, 0)::float AS rent_ref,
               COALESCE(commute_min, 0)::float AS commute_min
        FROM districts
        WHERE city_code = %s
        ORDER BY id
    """, (city_code,))


def list_all_districts() -> list[dict]:
    return _query("""
        SELECT d.city_code, d.name, d.lat, d.lng,
               COALESCE(d.price_sqm, 0)::float AS price_sqm,
               COALESCE(d.rent_ref, 0)::float AS rent_ref,
               COALESCE(d.commute_min, 0)::float AS commute_min
        FROM districts d
        ORDER BY d.city_code, d.id
    """)


def save_run(city_code, params: dict, summary: dict, version: str = "V7.0") -> None:
    """推演留痕：只存参数与结果摘要，不存任何身份信息。"""
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO simulation_runs (city_code, params, summary, app_version)
                    VALUES (%s, %s::jsonb, %s::jsonb, %s)
                """, (city_code, json.dumps(params, ensure_ascii=False),
                      json.dumps(summary, ensure_ascii=False), version))
    except Exception as e:  # noqa: BLE001
        logging.warning("simulation_runs 写入失败（不影响推演）：%s", e)
    finally:
        if conn is not None:
            conn.close()


def list_listings(city_code=None, max_price=None, min_area=None,
                  bedrooms=None, district_id=None, limit: int = 50) -> list[dict]:
    """V7.1：房源查询。没有坐标的房源用所属区位中心点兜底，并标记 geo_approximate。"""
    sql = """
        SELECT l.id, l.city_code, l.district_id,
               d.name AS district_name,
               l.community, l.title,
               l.total_price::float AS total_price,
               l.area_sqm::float    AS area_sqm,
               -- V8.6：套内面积与装修状态。列还没迁移时 to_jsonb 取值是 NULL，
               -- 前端/匹配层会自动按公摊率折算，不会因为"少一列"整体报错。
               (to_jsonb(l)->>'live_area_sqm')::float AS live_area_sqm,
               (to_jsonb(l)->>'decoration')           AS decoration,
               l.price_sqm::float   AS price_sqm,
               l.bedrooms,
               COALESCE(l.lat, d.lat) AS lat,
               COALESCE(l.lng, d.lng) AS lng,
               (l.lat IS NULL)        AS geo_approximate,
               CASE WHEN l.lat IS NOT NULL THEN 'exact'
                    WHEN d.id  IS NOT NULL THEN 'district'
                    ELSE 'none' END    AS geo_level,
               l.source, l.url, l.listed_at
          FROM listings l
          LEFT JOIN districts d ON d.id = l.district_id
         WHERE (%(city_code)s::text   IS NULL OR l.city_code = %(city_code)s)
           AND (%(max_price)s::numeric IS NULL OR l.total_price <= %(max_price)s)
           AND (%(min_area)s::numeric  IS NULL OR l.area_sqm  >= %(min_area)s)
           AND (%(bedrooms)s::int      IS NULL OR l.bedrooms  =  %(bedrooms)s)
           AND (%(district_id)s::bigint IS NULL OR l.district_id = %(district_id)s)
         ORDER BY l.total_price ASC NULLS LAST, l.id
         LIMIT %(limit)s
    """
    return _query(sql, {
        "city_code": city_code,
        "max_price": max_price,
        "min_area": min_area,
        "bedrooms": bedrooms,
        "district_id": district_id,
        "limit": max(1, min(int(limit or 50), 500)),
    })


def listing_stats(city_code=None) -> list[dict]:
    """V7.1：按城市聚合房源数量与价格区间，前端用来判断这个城市有没有真房源。"""
    return _query("""
        SELECT city_code,
               count(*)::int           AS n,
               min(total_price)::float AS min_price,
               max(total_price)::float AS max_price,
               avg(price_sqm)::float   AS avg_price_sqm,
               avg(area_sqm)::float    AS avg_area
          FROM listings
         WHERE (%(city_code)s::text IS NULL OR city_code = %(city_code)s)
         GROUP BY city_code
         ORDER BY n DESC
    """, {"city_code": city_code})


def health() -> dict:
    if not enabled():
        return {"enabled": False}
    try:
        row = _query("SELECT postgis_version() AS postgis, version() AS pg")[0]
        return {"enabled": True, "ok": True, **row}
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "ok": False, "error": str(e)}



# =====================================================================
# V9.1 匿名画像（user_profiles）
# ---------------------------------------------------------------------
# 只存 collect.normalize() 产出的一行；没有身份字段。
# 表在第一次写入时自动建（幂等），这样部署时不用记得先跑迁移。
# =====================================================================
_PROFILE_TABLE_READY = False

_PROFILE_DDL = """
CREATE TABLE IF NOT EXISTS user_profiles (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    kind            TEXT,
    app_version     TEXT,
    platform        TEXT,
    city_code       TEXT,
    city_tier       SMALLINT,
    family_size     SMALLINT,
    income_earners  SMALLINT,
    career_type     TEXT,
    industry        TEXT,
    income_band     TEXT,
    income_mid      INTEGER,
    expense_band    TEXT,
    savings_band    TEXT,
    house_total_wan NUMERIC(10,1),
    house_price_band TEXT,
    area_sqm        NUMERIC(8,1),
    rent_year       SMALLINT,
    loan_year       SMALLINT,
    down_strategy   TEXT,
    risk_mode       TEXT,
    buy_car         BOOLEAN,
    house_type      TEXT,
    feasible_count  INTEGER,
    result_state    TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_profiles_created ON user_profiles (created_at);
CREATE INDEX IF NOT EXISTS idx_user_profiles_city ON user_profiles (city_code);
CREATE INDEX IF NOT EXISTS idx_user_profiles_income ON user_profiles (income_band);
"""

_COLUMNS = ["session_id", "kind", "app_version", "platform", "city_code", "city_tier",
            "family_size", "income_earners", "career_type", "industry", "income_band",
            "income_mid", "expense_band", "savings_band", "house_total_wan",
            "house_price_band", "area_sqm", "rent_year", "loan_year", "down_strategy",
            "risk_mode", "buy_car", "house_type", "feasible_count", "result_state"]


def save_profile(row: dict) -> bool:
    """写入一行匿名画像。表不存在就先建；任何异常都只记日志，不影响接口。"""
    global _PROFILE_TABLE_READY
    if not enabled():
        return False
    conn = None
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                if not _PROFILE_TABLE_READY:
                    cur.execute(_PROFILE_DDL)
                    _PROFILE_TABLE_READY = True
                cols = [c for c in _COLUMNS if c in row]
                sql = ("INSERT INTO user_profiles (" + ", ".join(cols) + ") VALUES ("
                       + ", ".join(["%s"] * len(cols)) + ")")
                cur.execute(sql, tuple(row.get(c) for c in cols))
        return True
    except Exception as e:  # noqa: BLE001
        logging.warning("user_profiles 写入失败（不影响推演）：%s", e)
        return False
    finally:
        if conn is not None:
            conn.close()


def profile_summary(days: int = 90) -> dict:
    """聚合报告用的只读查询。days 控制回看窗口。"""
    days = max(1, min(int(days or 90), 3650))

    def q(sql, params=None):
        try:
            return _query(sql, params)
        except Exception as e:  # noqa: BLE001
            logging.warning("profile_summary 查询失败：%s", e)
            return []

    where = "WHERE created_at >= now() - make_interval(days => %s)"
    return {
        "totals": q(f"SELECT count(*)::int AS n, count(DISTINCT session_id)::int AS sessions, "
                    f"min(created_at) AS since FROM user_profiles {where}", (days,)),
        "by_city": q(f"SELECT city_code, count(*)::int AS n FROM user_profiles {where} "
                     f"GROUP BY city_code ORDER BY n DESC LIMIT 20", (days,)),
        "by_income": q(f"SELECT income_band, count(*)::int AS n, "
                       f"round(avg(house_total_wan), 1) AS avg_house_wan "
                       f"FROM user_profiles {where} GROUP BY income_band ORDER BY n DESC", (days,)),
        "by_career": q(f"SELECT career_type, count(*)::int AS n FROM user_profiles {where} "
                       f"GROUP BY career_type ORDER BY n DESC", (days,)),
        "by_industry": q(f"SELECT industry, count(*)::int AS n FROM user_profiles {where} "
                         f"GROUP BY industry ORDER BY n DESC", (days,)),
        "by_family": q(f"SELECT family_size, count(*)::int AS n FROM user_profiles {where} "
                       f"GROUP BY family_size ORDER BY family_size", (days,)),
        "by_price": q(f"SELECT house_price_band, count(*)::int AS n FROM user_profiles {where} "
                      f"GROUP BY house_price_band ORDER BY n DESC", (days,)),
        "by_intent": q(f"SELECT result_state, count(*)::int AS n FROM user_profiles {where} "
                       f"GROUP BY result_state", (days,)),
        "plan": q(f"SELECT round(avg(rent_year), 1) AS avg_rent_year, "
                  f"round(avg(loan_year), 1) AS avg_loan_year "
                  f"FROM user_profiles {where} AND result_state = 'feasible'", (days,)),
        "price_by_income": q(f"SELECT income_band, count(*)::int AS n, "
                             f"round(avg(house_total_wan), 1) AS avg_house_wan, "
                             f"round(avg(area_sqm), 1) AS avg_area "
                             f"FROM user_profiles {where} "
                             f"GROUP BY income_band ORDER BY avg_house_wan DESC", (days,)),
    }
