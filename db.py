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

