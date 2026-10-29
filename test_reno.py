# -*- coding: utf-8 -*-
"""V7.4 装修模型验收（纯计算，不需要 fastapi / 数据库）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reno  # noqa: E402

PASS = FAIL = 0


def ok(cond, name, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


print("=" * 66)
print("V7.4 装修成本模型")
print("=" * 66)

# 锚点：南宁（二线、薪资 7200、片区 10500、95㎡、标准档）
nn = reno.estimate_unit(7200, 10500, 3, 95)
ok(abs(nn - 1500) < 10, "南宁标准档 ≈1500 元/㎡（含面积修正）", round(nn, 1))

# 档次
simple = reno.estimate_unit(7200, 10500, 3, 95, "simple")
quality = reno.estimate_unit(7200, 10500, 3, 95, "quality")
ok(simple < nn < quality, "简装 < 标准 < 品质", (round(simple), round(nn), round(quality)))
ok(abs(simple / nn - 0.75) < 0.01, "简装系数 0.75", simple / nn)
ok(abs(quality / nn - 1.45) < 0.01, "品质系数 1.45", quality / nn)

# 空间差异
sz = reno.estimate_unit(13000, 58000, 1, 70)
county = reno.estimate_unit(5000, 6000, 4, 110)
ok(sz > nn > county, "深圳 > 南宁 > 县城", (round(sz), round(nn), round(county)))
ok(1500 < sz < 4200, "深圳单价落在合理区间", sz)
ok(500 < county < 1200, "县城单价落在合理区间", county)

# 单调性
ok(reno.estimate_unit(20000, 10500, 3, 95) > nn, "薪资越高单价越高")
ok(reno.estimate_unit(7200, 30000, 3, 95) > nn, "片区价越高单价越高")
ok(reno.estimate_unit(7200, 10500, 3, 40) > nn, "小户型单价更高（<60㎡ 更贵）")

# 边界
ok(reno.estimate_unit(1, 1, 4, 1) >= reno.UNDER_FLOOR, "下限兜底")
ok(reno.estimate_unit(999999, 999999, 1, 1) <= reno.CEILING, "上限封顶")

# 总价
total = reno.estimate_total(7200, 10500, 3, 95)
ok(abs(total - nn * 95) < 1, "总价 = 单价 × 面积", total)
ok(12e4 < total < 16e4, "南宁 95㎡ 全包装修落在 12~16 万", round(total / 10000, 1))

print("\n同时对照豆包给的市场区间（南宁 95㎡ 全包）：")
print(f"  简装 800~1200 → 模型 {round(simple):>4} 元/㎡  "
      f"{'✅' if 700 <= simple <= 1250 else '⚠️'}")
print(f"  中档 1300~1800 → 模型 {round(nn):>4} 元/㎡ "
      f"{'✅' if 1300 <= nn <= 1800 else '⚠️'}")
print(f"  品质 1800~2500 → 模型 {round(quality):>4} 元/㎡ "
      f"{'✅' if 1700 <= quality <= 2600 else '⚠️'}")

print("\n" + "=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 66)
sys.exit(1 if FAIL else 0)
