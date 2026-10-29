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


def health() -> dict:
    if not enabled():
        return {"enabled": False}
    try:
        row = _query("SELECT postgis_version() AS postgis, version() AS pg")[0]
        return {"enabled": True, "ok": True, **row}
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "ok": False, "error": str(e)}

