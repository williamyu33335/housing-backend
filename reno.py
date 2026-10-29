# -*- coding: utf-8 -*-
"""
V7.4 装修成本模型（独立模块，不依赖 fastapi / pydantic，可单独测）
--------------------------------------------------------------------------
为什么单独一个文件：
  · 装修是一个"政策"，不是引擎的一部分；将来要调系数，只该动这里；
  · tests/test_reno.py 要能在没有 Web 依赖的环境里直接跑。

口径（和豆包核过的市场数据一致）：
  · 单价按**建筑面积**算，含硬装 + 基础主材，**不含家具家电**；
  · 锚点：南宁 2026 年全包（自住中档）约 1500 元/㎡，是主流成交价，不是套餐最低价；
  · 半包 700~900、简装全包 800~1200、品质 1800~2500、豪装 2500+ —— 用档次系数表达。
"""
from __future__ import annotations

# 锚点：南宁、二线、90㎡、标准档
BASE_UNIT = 1500.0
ANCHOR_SALARY = 7200.0
ANCHOR_PRICE = 10500.0
ANCHOR_AREA = 90.0

TIER_FACTOR = {1: 1.15, 2: 1.05, 3: 1.00, 4: 0.90}
LEVEL_FACTOR = {"simple": 0.75, "standard": 1.0, "quality": 1.45}

UNDER_FLOOR = 600.0
CEILING = 4200.0


def estimate_unit(avg_salary: float, price_sqm: float, city_tier: int,
                  area_sqm: float, level: str = "standard") -> float:
    """装修单价（元/㎡，建筑面积口径，不含家具家电）。

    三个指数都小于 1：装修大头是人工和辅材，不会随城市薪资/房价线性放大。
      · 薪资 ^0.5   人工贵一点，但不是翻倍
      · 房价 ^0.25  房价传导到装修最弱
      · 面积 ^-0.08 大户型单位成本略低（60㎡ 以下小户型实际更贵，这里只是线性近似）
    """
    k_salary = (max(float(avg_salary), 1.0) / ANCHOR_SALARY) ** 0.5
    k_price = (max(float(price_sqm), 1.0) / ANCHOR_PRICE) ** 0.25
    k_tier = TIER_FACTOR.get(int(city_tier or 3), 1.0)
    k_area = (max(float(area_sqm), 1.0) / ANCHOR_AREA) ** -0.08
    k_level = LEVEL_FACTOR.get(level, 1.0)
    unit = BASE_UNIT * k_salary * k_price * k_tier * k_area * k_level
    return max(UNDER_FLOOR, min(CEILING, unit))


def estimate_total(avg_salary: float, price_sqm: float, city_tier: int,
                   area_sqm: float, level: str = "standard") -> float:
    """装修总价（元）= 单价 × 建筑面积。"""
    return estimate_unit(avg_salary, price_sqm, city_tier, area_sqm, level) * max(float(area_sqm), 1.0)
