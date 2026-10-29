"""
把 cities_master.json + 南宁真实片区 灌进 PostgreSQL（V7.0）
--------------------------------------------------------------------------
用法（PowerShell）：
    $env:DATABASE_URL = "postgresql://postgres:密码@127.0.0.1:5432/housing"
    python 数据/import_to_db.py

幂等：重复执行会 UPDATE 而不是报错。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name in ("数据", "data") else HERE


def load_json(name: str):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def main() -> int:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("请先设置 DATABASE_URL，例如：\n"
                         '  $env:DATABASE_URL = "postgresql://postgres:密码@127.0.0.1:5432/housing"')
    master = load_json("cities_master.json")
    nanning = load_json("nanning_districts.json") if (HERE / "nanning_districts.json").exists() else []
    bench = load_json("market_benchmarks.json")

    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        # ---------- 城市 ----------
        cities = master["cities"]
        execute_values(cur, """
            INSERT INTO cities (code, name, tier, tier_label, lat, lng, price_sqm, rent_sqm, avg_salary, source)
            VALUES %s
            ON CONFLICT (code) DO UPDATE SET
              name = EXCLUDED.name, tier = EXCLUDED.tier, tier_label = EXCLUDED.tier_label,
              lat = EXCLUDED.lat, lng = EXCLUDED.lng, price_sqm = EXCLUDED.price_sqm,
              rent_sqm = EXCLUDED.rent_sqm, avg_salary = EXCLUDED.avg_salary,
              source = EXCLUDED.source, updated_at = now()
        """, [(c["code"], c["name"], c["tier"], c["tier_label"], c["lat"], c["lng"],
               c["price_sqm"], c["rent_sqm"], c["avg_salary"], c["source"]) for c in cities])

        cur.execute("""
            UPDATE cities
               SET geom = ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography
             WHERE geom IS NULL
        """)

        # ---------- 区县 / 区位 ----------
        rows = []
        for c in cities:
            if c["name"] == "南宁" and nanning:
                for d in nanning:
                    rows.append((c["code"], d["key"], d["name"], d["lat"], d["lng"],
                                 d["price_sqm"], d["rent_ref"], d["commute_min"]))
            else:
                for z in c.get("zones", []):
                    rows.append((c["code"], z["key"], z["name"], z["lat"], z["lng"],
                                 z["price_sqm"], z["rent_ref"], z["commute_min"]))

        for code, key, name, lat, lng, price, rent, commute in rows:
            cur.execute("""
                INSERT INTO districts (city_code, zone_key, name, lat, lng, price_sqm, rent_ref, commute_min)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (city_code, name) DO UPDATE SET
                  zone_key = EXCLUDED.zone_key, lat = EXCLUDED.lat, lng = EXCLUDED.lng,
                  price_sqm = EXCLUDED.price_sqm, rent_ref = EXCLUDED.rent_ref,
                  commute_min = EXCLUDED.commute_min
            """, (code, key, name, lat, lng, price, rent, commute))

        cur.execute("""
            UPDATE districts
               SET geom = ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography
             WHERE geom IS NULL
        """)

        # ---------- 外部基准 ----------
        asof = (bench.get("as_of") or "2025-11") + "-01" if len(bench.get("as_of", "")) == 7 else None
        for s in bench.get("sources", []):
            for metric, value in s.get("metrics", {}).items():
                cur.execute("""
                    INSERT INTO benchmarks (metric, value, unit, asof, publisher, source_url)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (metric, asof, publisher) DO UPDATE SET
                      value = EXCLUDED.value, source_url = EXCLUDED.source_url
                """, (metric, value, None, asof, s.get("publisher"), s.get("url")))

        conn.commit()

        cur.execute("SELECT count(*) FROM cities")
        n_cities = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM districts")
        n_districts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM benchmarks")
        n_bench = cur.fetchone()[0]
        print(f"✅ 导入完成：cities={n_cities}  districts={n_districts}  benchmarks={n_bench}")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
