# -*- coding: utf-8 -*-
"""
周边便利分（V8.10 · 纯函数，不碰网络、不依赖 fastapi）
==========================================================================
为什么单独一个文件：
    便利分原来写在前端 index.html 里，Python 这边只能"信任它算得对"。
    可它已经出过两次事故——一次是绝对值封顶（青秀 14 个地铁站，
    六个区全 100 分，等于没算），一次是区中心选错（青秀中心落在
    青秀山边上，量出"地铁 1202 米"）。这类错只能靠单测守。

这一版把规则收回来，只有一处实现：
    · 数据采集脚本用本模块算好分数，写进 JSON（片区级 / 房源级各一份）；
    · 前端只读分数，不再自己算（旧数据没有 scores 字段时才回退到本地公式）。
    · 以后接 PostGIS 预计算，也是调这一份规则，不会出现两套口径。

三条纪律：
    1. 没采到的类目不计入分母。缺数据 => 这一项不参与打分，
       而不是"当成 0 分"——那是拿空白冒充结论。
    2. 采到了但半径为 0 命中，才是真的 0 分（"1 公里内确实没有"）。
    3. 负分（闹静比）有上限。便利分只是排序里的润色项，
       预算永远是第一约束；让噪音扣分压过价格贴合度，就是本末倒置。
"""
from __future__ import annotations

from typing import Any

# 类目：cap 是"超过这个距离就不算便利"的上限（米），和采集半径一致。
CATS: dict[str, dict[str, Any]] = {
    "metro": {"label": "地铁站", "cap": 3000, "kind": "positive"},
    "school": {"label": "小学", "cap": 2500, "kind": "positive"},
    "hospital": {"label": "医院", "cap": 3000, "kind": "positive"},
    "mall": {"label": "购物中心", "cap": 3000, "kind": "positive"},
    "park": {"label": "公园", "cap": 2000, "kind": "positive"},
    "market": {"label": "菜市场", "cap": 2000, "kind": "positive"},
    "food": {"label": "美食街", "cap": 2500, "kind": "positive"},
    # 夜市是"高干扰"设施：它不是便利，太近就是吵。所以单独建类目，
    # 只用来扣分，不进便利分的分子。
    "night": {"label": "夜市", "cap": 1500, "kind": "negative"},
}

# 三套偏好模板只改权重，不改规则——用户切的是"在乎什么"，不是"标准"。
PREFS: dict[str, dict[str, Any]] = {
    "commute": {
        "label": "通勤优先",
        "w": {"metro": 6, "school": 1.2, "hospital": 1.2, "mall": 1,
              "park": 0.6, "market": 1, "food": 0.6},
    },
    "family": {
        "label": "家庭刚需",
        "w": {"metro": 2, "school": 4.5, "hospital": 3, "mall": 1.2,
              "park": 2, "market": 3, "food": 1},
    },
    "leisure": {
        "label": "宜居休闲",
        "w": {"metro": 1.5, "school": 1, "hospital": 2, "mall": 1,
              "park": 4.5, "market": 2.2, "food": 2},
    },
}

DEFAULT_PREF = "commute"

# 闹静比：贴脸是噪音，不是便利。阈值按"步行 2~4 分钟"这个体感定，
# 不是拍脑袋的玄学——豆包的原话是"只有高干扰设施才该扣分"。
PENALTY_RULES: dict[str, list[tuple[int, int]]] = {
    "mall": [(150, 10), (300, 4)],
    "food": [(120, 8), (250, 3)],
    "night": [(200, 12), (400, 6)],
}
PENALTY_CAP = 20


def _num(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def decayed(dist, cap) -> float:
    """距离衰减：门口 1 分，越远越低，超过上限 0 分。"""
    d = _num(dist)
    if d is None or d < 0 or cap <= 0:
        return 0.0
    if d >= cap:
        return 0.0
    return 1.0 - d / cap


def _present(row: dict, key: str) -> bool:
    """这一行到底采没采到这个类目。missing 与 near=None 是两件事。"""
    if not isinstance(row, dict):
        return False
    if key not in row:
        return False
    cell = row.get(key)
    return isinstance(cell, dict)


def penalty(row: dict) -> int:
    """闹静扣分（正数，调用方减）。有上限，不让它盖过预算。"""
    total = 0
    for key, rules in PENALTY_RULES.items():
        if not _present(row, key):
            continue
        d = _num((row.get(key) or {}).get("near"))
        if d is None or d < 0:
            continue
        for limit, points in rules:      # 从最严的档开始比
            if d < limit:
                total += points
                break
    return min(total, PENALTY_CAP)


def score(row: dict, pref: str = DEFAULT_PREF) -> int | None:
    """0~100 的便利分。这一行没有可用类目时返回 None（不是 0）。"""
    if not isinstance(row, dict):
        return None
    w = (PREFS.get(pref) or PREFS[DEFAULT_PREF])["w"]

    got = 0.0
    total_w = 0.0
    for key, weight in w.items():
        if not _present(row, key):
            continue                      # 没采到 -> 不进分母
        cap = CATS[key]["cap"]
        total_w += weight
        d = _num((row.get(key) or {}).get("near"))
        got += weight * decayed(d, cap)

    if total_w <= 0:
        return None
    raw = got / total_w * 100.0
    return max(0, min(100, round(raw - penalty(row))))


def scores_all(row: dict) -> dict[str, int]:
    """三套偏好一次算完，落库/落 JSON 时只存一份，前端切偏好不用重算。"""
    out: dict[str, int] = {}
    for key in PREFS:
        s = score(row, key)
        if s is not None:
            out[key] = s
    return out


def attach_scores(row: dict) -> dict:
    """原地补 scores 字段并返回（采集脚本用）。"""
    if isinstance(row, dict):
        s = scores_all(row)
        if s:
            row["scores"] = s
    return row


def nearest_summary(row: dict) -> list[dict]:
    """给详情弹窗用的逐项清单：类目、最近距离、半径内数量。

    `near=None` 且 `n=0` 表示"问过了，这个范围内确实没有"，
    不能显示成"暂无数据"；只有类目缺失才是"暂无数据"。
    """
    out = []
    if not isinstance(row, dict):
        return out
    for key, meta in CATS.items():
        if not _present(row, key):
            continue
        cell = row.get(key) or {}
        out.append({
            "key": key,
            "label": meta["label"],
            "near": _num(cell.get("near")),
            "n": _num(cell.get("n")),
            "kind": meta["kind"],
        })
    return out
