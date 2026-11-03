# -*- coding: utf-8 -*-
"""
高德 Web 服务客户端（V8.7 · 周边设施数据的唯一入口）
--------------------------------------------------------------------------
为什么单独一个模块：和 reno.py / listings_match.py 一样，它必须能脱离
fastapi 和数据库单独测。网络会断、配额会满、key 会过期——这些都不是
"业务逻辑错误"，但如果不隔离，就会变成页面上莫名其妙的空白。

三条纪律：
1. **key 只从环境变量读**（AMAP_KEY）。绝不写进仓库、绝不写进 index.html。
   Web 服务 key 一旦进前端，等于把配额送给全世界。
2. **失败返回 None，不返回空列表**。
   None = 没采集到（要显示"暂无数据"）；[] = 采集到了，周边真的一所都没有。
   这两种在界面上是完全不同的两句话，混用就是撒谎。
3. **有缓存，带采集时间**。周边设施变化很慢，没必要每次都问；
   但缓存里必须写清"这是哪天采的"，不能让人以为是实时数据。

配额提醒：个人开发者每日调用量有限（见控制台「配额」页），所以
默认限速 0.35s/次，且批量采集走缓存。
"""
from __future__ import annotations

# 2. 然后才是标准库
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

# 3. 再然后是第三方库和我们自己的加载逻辑
# key 的来源，优先级从高到低：
#   1) 环境变量 AMAP_KEY —— Render 上必须走这个（密码/ 在 .gitignore 里，不会上传）
#   2) 本地 密码/gaode.env —— 开发机专用
#   3) 都没有 → enabled() = False，采集脚本明确报错，绝不静默产出 0
# 注意：python-dotenv 是**可选**依赖。没有它不能让整个后端 import 失败——
#      那是"一个本地便利功能拖垮线上服务"的经典事故。
try:
    from dotenv import load_dotenv
except ImportError:      # pragma: no cover
    load_dotenv = None

if load_dotenv is not None:
    _env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "密码", "gaode.env")
    try:
        load_dotenv(_env_file)
    except Exception:    # noqa: BLE001
        pass

BASE = "https://restapi.amap.com/v3"
DEFAULT_QPS_INTERVAL = 0.35          # 两次请求之间至少隔这么久
DEFAULT_TTL_DAYS = 30                # 周边设施的缓存有效期
CN_TZ = timezone(timedelta(hours=8))

# 高德错误码 → 人话。前端/脚本直接显示这个，不显示错误码。
ERRORS = {
    "10001": "key 无效：确认是「Web服务」类型的 key，且账号已实名认证",
    "10002": "key 已过期或被删除",
    "10003": "今日调用量已用完",
    "10004": "请求过于频繁（单位时间超限）",
    "10005": "IP 白名单限制：这个 IP 不在允许列表里",
    "10009": "key 与绑定的服务平台不符（Web服务 key 不能用于 JS API）",
    "10044": "账号维度日调用量超限",
    "20000": "请求参数错误",
}

# 周边设施：用关键词搜而不是分类码。
# 原因：分类码（如 150500 地铁站）一旦记错，结果会安静地变成 0，
# 而关键词搜错了至少看得出来（数量明显不对）。等 key 通了再用分类码校准。
# 片区级：3km。理由见上——1km 量的是"中心点周围"，不是"这个片区"。
# 房源级以后用 1km（DISTRICT_CATEGORIES 留作对照）。
CATEGORIES = {
    "metro": {"label": "地铁站", "keywords": "地铁站", "radius": 3000},
    "school": {"label": "小学", "keywords": "小学", "radius": 3000},
    "hospital": {"label": "医院", "keywords": "医院", "radius": 3000},
    "mall": {"label": "购物中心", "keywords": "购物中心", "radius": 3000},
}

# 房源级（有了真实坐标再用）：1km，这才是"楼下"
LISTING_CATEGORIES = {
    "metro": {"label": "地铁站", "keywords": "地铁站", "radius": 1000},
    "school": {"label": "小学", "keywords": "小学", "radius": 1000},
    "hospital": {"label": "医院", "keywords": "医院", "radius": 1000},
    "mall": {"label": "购物中心", "keywords": "购物中心", "radius": 1000},
}

_last_error: str | None = None
_last_call_ts = 0.0


def api_key() -> str:
    return (os.getenv("AMAP_KEY") or "").strip()


def enabled() -> bool:
    return bool(api_key())


def last_error() -> str | None:
    return _last_error


def _now_iso() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%dT%H:%M:%S%z")


def _cache_path(cache_file: str | None) -> str | None:
    if cache_file:
        return cache_file
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "数据", "amap_cache.json")


def _load_cache(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(path: str | None, cache: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass    # 缓存写不进去不影响主流程


def _http(path: str, params: dict) -> dict | None:
    """发一次请求。任何失败都返回 None 并记下 last_error（人话）。"""
    global _last_error, _last_call_ts
    key = api_key()
    if not key:
        _last_error = "没有配置 AMAP_KEY（服务端起一个环境变量即可）"
        return None

    gap = time.time() - _last_call_ts
    if gap < DEFAULT_QPS_INTERVAL:
        time.sleep(DEFAULT_QPS_INTERVAL - gap)
    _last_call_ts = time.time()

    q = dict(params)
    q["key"] = key
    url = f"{BASE}/{path}?{urllib.parse.urlencode(q)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        _last_error = f"网络请求失败：{type(e).__name__}"
        return None

    if str(payload.get("status")) != "1":
        code = str(payload.get("infocode") or "")
        _last_error = ERRORS.get(code) or f"高德返回错误 {code}：{payload.get('info')}"
        return None

    _last_error = None
    return payload


def poi_count(lat: float, lng: float, keywords: str, radius: int = 1000,
              cache_file: str | None = None, ttl_days: int = DEFAULT_TTL_DAYS,
              use_cache: bool = True) -> int | None:
    """某个坐标半径内，关键词命中的 POI 数量。

    返回 None 表示"没问到"（没 key / 超配额 / 网络断了），
    返回 0 表示"问到了，周边确实是 0 个"。调用方必须区分。
    """
    if lat is None or lng is None:
        globals()["_last_error"] = "坐标为空"
        return None

    cache_path = _cache_path(cache_file)
    ck = f"poi|{lat:.4f},{lng:.4f}|{keywords}|{radius}"
    cache = _load_cache(cache_path) if use_cache else {}
    hit = cache.get(ck)
    if hit:
        try:
            at = datetime.fromisoformat(hit["at"])
            if datetime.now(CN_TZ) - at < timedelta(days=ttl_days):
                return int(hit["count"])
        except Exception:  # noqa: BLE001
            pass

    payload = _http("place/around", {
        "location": f"{lng:.6f},{lat:.6f}",
        "keywords": keywords,
        "radius": radius,
        "offset": 1,        # 只要总数，不拉明细，省流量也省解析
        "page": 1,
        "extensions": "base",
    })
    if payload is None:
        return None

    try:
        count = int(payload.get("count") or 0)
    except (TypeError, ValueError):
        return None

    if use_cache:
        cache[ck] = {"at": _now_iso(), "count": count,
                     "lat": round(lat, 4), "lng": round(lng, 4),
                     "keywords": keywords, "radius": radius}
        _save_cache(cache_path, cache)
    return count


def nearby_counts(lat: float, lng: float, cache_file: str | None = None,
                  categories: dict | None = None) -> dict | None:
    """一个坐标的周边设施汇总。任何一类没问到，整条返回 None（不做部分填充）。"""
    cats = categories or CATEGORIES
    out: dict[str, Any] = {}
    for name, cfg in cats.items():
        n = poi_count(lat, lng, cfg["keywords"], cfg["radius"], cache_file=cache_file)
        if n is None:
            return None
        out[name] = n
    out["radius_m"] = max(c["radius"] for c in cats.values())
    out["captured_at"] = _now_iso()
    return out
