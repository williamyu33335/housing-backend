"""
V9.1 匿名画像采集：把一次推演变成一行可以统计的数据
--------------------------------------------------------------------------
原则（写在代码里，防止以后漂移）：
1. 不收集身份信息：没有姓名、手机号、微信、IP、设备号、精确坐标。
2. 收入不存精确值，只存区间 + 区间中点；区间中点只用于画分布。
3. 城市只到行政区，不到小区；区县都不存，只存 city_code。
4. 客户端可以随时关闭（前端 localStorage 的 jw_collect=off），服务端不做任何反制。
5. 这份数据只用于聚合报告与产品决策，不用于给个人推广告。

这个模块是纯函数，不依赖 fastapi / psycopg2，方便单测。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

# 收入区间（元/月）：左闭右开，最后一段无上限
INCOME_BANDS = [(3000, "<3k"), (5000, "3-5k"), (8000, "5-8k"), (12000, "8-12k"),
                (20000, "12-20k"), (30000, "20-30k"), (None, "30k+")]
EXPENSE_BANDS = [(1500, "<1.5k"), (2500, "1.5-2.5k"), (4000, "2.5-4k"),
                 (6000, "4-6k"), (None, "6k+")]
SAVINGS_BANDS = [(10, "<10万"), (30, "10-30万"), (60, "30-60万"),
                 (100, "60-100万"), (200, "100-200万"), (None, "200万+")]
PRICE_BANDS = [(50, "<50万"), (80, "50-80万"), (120, "80-120万"), (200, "120-200万"),
               (300, "200-300万"), (500, "300-500万"), (None, "500万+")]

CAREERS = {"public", "corporate", "freelance"}
INDUSTRIES = {"it", "finance", "research", "medical", "education", "public_admin",
              "manufacturing", "hospitality", "other"}
RISK_MODES = {"steady", "aggressive"}
HOUSE_TYPES = {"new", "used", "unspecified"}
KINDS = {"simulate", "no_scheme"}

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return f


def band_of(value: Any, bands, unit: str = "") -> Optional[str]:
    v = _num(value)
    if v is None or v < 0:
        return None
    for limit, label in bands:
        if limit is None or v < limit:
            return label + unit
    return None


def band_mid(value: Any, bands) -> Optional[int]:
    """区间中点：只用于画分布，不能反推出某个人的精确收入。"""
    v = _num(value)
    if v is None or v < 0:
        return None
    for i, (limit, _label) in enumerate(bands):
        if limit is None or v < limit:
            lo = 0 if i == 0 else bands[i - 1][0]
            if limit is None:
                return int(max(v, lo))
            return int((lo + limit) / 2)
    return None


def _pick(value: Any, allowed: set, default: str = "other") -> str:
    s = str(value or "").strip().lower()
    return s if s in allowed else default


def _clamp_int(value: Any, lo: int, hi: int) -> Optional[int]:
    v = _num(value)
    if v is None:
        return None
    return max(lo, min(hi, int(round(v))))


def _clean_session(sid: Any) -> Optional[str]:
    s = str(sid or "").strip()
    return s if _SESSION_RE.match(s) else None


def normalize(params: Dict[str, Any], result: Optional[Dict[str, Any]] = None,
              meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把前端提交的原始参数压成一行匿名画像。调用方负责落库。"""
    params = params or {}
    result = result or {}
    meta = meta or {}

    top = (result.get("top_comprehensive") or [{}])[0] if isinstance(result, dict) else {}
    all_scheme = result.get("all_scheme") if isinstance(result, dict) else None
    feasible = (len(all_scheme) if isinstance(all_scheme, list)
                else _clamp_int(result.get("all_scheme_count"), 0, 100000))
    kind = str(meta.get("kind") or "simulate")
    if kind not in KINDS:
        kind = "simulate"

    income = _num(params.get("monthly_income_net"))
    expense = _num(params.get("monthly_expense"))
    price = _num(params.get("house_total_wan"))

    row: Dict[str, Any] = {
        "session_id": _clean_session(meta.get("session_id")),
        "kind": kind,
        "app_version": str(meta.get("app_version") or "")[:32] or None,
        "platform": str(meta.get("platform") or "web")[:16],
        "city_code": str(params.get("city_code") or "")[:12] or None,
        "city_tier": _clamp_int(params.get("city_tier"), 1, 5),
        "family_size": _clamp_int(params.get("family_size"), 1, 10),
        "income_earners": _clamp_int(params.get("income_earners"), 1, 4),
        "career_type": _pick(params.get("career_type"), CAREERS),
        "industry": _pick(params.get("industry"), INDUSTRIES),
        "income_band": band_of(income, INCOME_BANDS),
        "income_mid": band_mid(income, INCOME_BANDS),
        "expense_band": band_of(expense, EXPENSE_BANDS),
        "savings_band": band_of(_num(params.get("initial_savings_wan")), SAVINGS_BANDS),
        "house_total_wan": round(price, 1) if price is not None else None,
        "house_price_band": band_of(price, PRICE_BANDS),
        "area_sqm": round(_num(params.get("target_area_sqm")) or 0, 1) or None,
        "rent_year": _clamp_int(top.get("rent_year"), 0, 20),
        "loan_year": _clamp_int(top.get("loan_year"), 1, 30),
        "down_strategy": str(top.get("down_strategy") or "")[:12] or None,
        "risk_mode": _pick(params.get("risk_mode"), RISK_MODES, default="steady"),
        "buy_car": bool(params.get("buy_car")),
        "house_type": _pick(params.get("house_type"), HOUSE_TYPES, default="unspecified"),
        "feasible_count": feasible,
        "result_state": "no_scheme" if (kind == "no_scheme" or feasible == 0) else "feasible",
    }
    if row["rent_year"] is None and result.get("best"):
        row["rent_year"] = _clamp_int(result["best"].get("rent_year"), 0, 20)
    return row


def describe() -> Dict[str, Any]:
    """给 /api/collect 的 GET 用：说明收什么、不收什么。"""
    return {
        "collect": ["城市编码/能级", "家庭人数/就业人数", "职业/行业", "收入区间", "支出区间",
                    "存款区间", "目标房价区间", "推荐租期/贷期/首付策略", "风险姿态", "是否有车",
                    "新房或二手房", "可行方案数量", "App 版本与平台"],
        "never": ["姓名", "手机号", "微信", "IP", "设备号", "身份证", "精确坐标", "小区门牌"],
        "income": "只存区间与区间中点，不存精确到元的收入",
        "opt_out": "前端「我的 → 匿名数据」可随时关闭；关闭后客户端不再发送",
    }
