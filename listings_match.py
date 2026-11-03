# -*- coding: utf-8 -*-
"""
房源匹配（V8.6 · 纯函数，不碰数据库、不依赖 fastapi）
--------------------------------------------------------------------------
为什么单独一个文件：reno.py 当年能脱离 FastAPI 单独测，救了后面无数次返工。
匹配规则一旦和 HTTP 层粘在一起，就只敢在真库上试——而真库只有 34 套。

这里的唯一职责：**给定一批真实房源行 + 用户预算，说清楚"有没有对得上的"**。
没有就返回空列表和原因，绝不凑一个"最接近的推荐"冒充匹配结果。
最接近的几套可以给，但必须单独放在 nearest 里，并标清楚"不是匹配"。

字段口径（和 db.list_listings 对齐）：
    total_price  总价（元）
    area_sqm     建筑面积（房产证上那个数）
    price_sqm    单价（元/㎡，建面口径）
    live_area_sqm 套内面积（可选；没有就按公摊率折算）
"""
from __future__ import annotations

from typing import Any


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def live_area_of(row: dict, public_share: float) -> float:
    """套内面积：有 live_area_sqm 就用真值，没有才按公摊率折算。

    为什么优先用真值：公摊率是我们按城市档次估的，房源如果自带实测套内，
    那不是"估得准不准"的问题，是"别拿估算覆盖实测"。
    """
    live = _num(row.get("live_area_sqm"))
    if live > 0:
        return live
    built = _num(row.get("area_sqm"))
    if built <= 0:
        return 0.0
    return built * max(0.55, 1.0 - public_share)


def _decorate(row: dict, public_share: float, budget_wan: float, target_area: float) -> dict:
    total = _num(row.get("total_price"))
    live = live_area_of(row, public_share)
    price_wan = total / 10000.0
    out: dict[str, Any] = {
        "id": row.get("id"),
        "community": row.get("community") or row.get("title") or "未命名小区",
        "title": row.get("title"),
        "district_name": row.get("district_name"),
        "total_price": total,
        "price_wan": round(price_wan, 2),
        "area_sqm": round(_num(row.get("area_sqm")), 1),
        "live_area_sqm": round(live, 1),
        "price_sqm": round(_num(row.get("price_sqm")), 0),
        "bedrooms": row.get("bedrooms"),
        "decoration": row.get("decoration"),
        "source": row.get("source"),
        "url": row.get("url"),
        "listed_at": str(row.get("listed_at")) if row.get("listed_at") else None,
        "geo_level": row.get("geo_level"),
        "geo_approximate": bool(row.get("geo_approximate")),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
    }
    if budget_wan > 0:
        out["price_delta_pct"] = round((price_wan - budget_wan) / budget_wan * 100.0, 1)
    if target_area > 0 and live > 0:
        out["area_delta_pct"] = round((live - target_area) / target_area * 100.0, 1)
    return out


def match_listings(rows, budget_wan: float = 0.0, target_area_sqm: float = 0.0,
                   public_share: float = 0.15, price_tol: float = 0.10,
                   area_tol: float = 0.15, limit: int = 20) -> dict:
    """在真实房源里按"总价±10% / 套内±15%"找对得上的房子。

    budget_wan=0 表示用户没填总价（只看面积）；target_area_sqm=0 表示没填面积。
    两个都填 → 双条件；只填一个 → 单条件；都不填 → 不猜，直接说不知道要找什么。
    """
    budget_wan = _num(budget_wan)
    target_area_sqm = _num(target_area_sqm)
    price_tol = max(0.02, min(_num(price_tol, 0.10), 0.5))
    area_tol = max(0.05, min(_num(area_tol, 0.15), 0.5))
    public_share = max(0.0, min(_num(public_share, 0.15), 0.45))

    pool = []
    for r in rows or []:
        if _num(r.get("total_price")) > 0:
            pool.append(r)

    result: dict[str, Any] = {
        "matched": [],
        "count": 0,
        "pool": {"n": len(pool)},
        "band": {},
        "reason": None,
        "message": "",
        "nearest": [],
    }

    if not pool:
        result["reason"] = "no_pool"
        result["message"] = "这个城市还没有入库房源。我们不编数据，所以这里就是空的。"
        return result

    prices = [_num(r["total_price"]) / 10000.0 for r in pool]
    lives = [live_area_of(r, public_share) for r in pool]
    lives = [x for x in lives if x > 0]
    result["pool"].update({
        "min_wan": round(min(prices), 1),
        "max_wan": round(max(prices), 1),
    })
    if lives:
        result["pool"].update({"min_live": round(min(lives), 1), "max_live": round(max(lives), 1)})

    if budget_wan <= 0 and target_area_sqm <= 0:
        result["reason"] = "no_budget"
        result["message"] = "先填「房屋总价」或「想住多大」，我才能去库里找对得上的房子。"
        return result

    price_lo, price_hi = (0.0, float("inf"))
    if budget_wan > 0:
        price_lo = budget_wan * (1.0 - price_tol)
        price_hi = budget_wan * (1.0 + price_tol)
        result["band"]["price_min_wan"] = round(price_lo, 1)
        result["band"]["price_max_wan"] = round(price_hi, 1)

    area_lo, area_hi = (0.0, float("inf"))
    if target_area_sqm > 0:
        area_lo = target_area_sqm * (1.0 - area_tol)
        area_hi = target_area_sqm * (1.0 + area_tol)
        result["band"]["area_min"] = round(area_lo, 1)
        result["band"]["area_max"] = round(area_hi, 1)

    hits = []
    for r in pool:
        price_wan = _num(r["total_price"]) / 10000.0
        if not (price_lo <= price_wan <= price_hi):
            continue
        live = live_area_of(r, public_share)
        if target_area_sqm > 0 and not (area_lo <= live <= area_hi):
            continue
        item = _decorate(r, public_share, budget_wan, target_area_sqm)
        # 打分：两个条件都填时，价格和面积各占一半；只填一个就只看那个
        score = 0.0
        if budget_wan > 0:
            score += abs(item.get("price_delta_pct", 0)) * (0.5 if target_area_sqm > 0 else 1.0)
        if target_area_sqm > 0:
            score += abs(item.get("area_delta_pct", 0)) * (0.5 if budget_wan > 0 else 1.0)
        item["_score"] = score
        hits.append(item)

    hits.sort(key=lambda x: x["_score"])
    for h in hits:
        h.pop("_score", None)

    result["matched"] = hits[:max(1, min(int(limit or 20), 50))]
    result["count"] = len(hits)

    if not hits:
        result["reason"] = "no_match"
        lo = result["pool"]["min_wan"]
        hi = result["pool"]["max_wan"]
        msg = f"库里 {len(pool)} 套在售，总价 {lo}~{hi} 万"
        if "min_live" in result["pool"]:
            msg += f"、套内 {result['pool']['min_live']}~{result['pool']['max_live']} ㎡"
        if budget_wan > 0:
            msg += f"；你要的 {round(budget_wan, 1)} 万（±{int(price_tol * 100)}%）里没有对得上的"
        if target_area_sqm > 0:
            msg += f"；套内 {round(target_area_sqm, 0)} ㎡（±{int(area_tol * 100)}%）也没有"
        result["message"] = msg + "。"
        # 最接近的 3 套：单独给，名字就叫 nearest，不叫推荐
        near = sorted(pool, key=lambda r: abs(_num(r["total_price"]) / 10000.0 - budget_wan)
                      if budget_wan > 0 else abs(live_area_of(r, public_share) - target_area_sqm))
        result["nearest"] = [_decorate(r, public_share, budget_wan, target_area_sqm) for r in near[:3]]

    return result
