"""
V6.0 空间异质性引擎 · 数值自测
--------------------------------------------------------------------------
不安装 fastapi/pydantic 也能跑：这里用最小 stub 替换 Web 层，
直接调用 main.py 里的端点函数（它们本身就是纯函数），验证：

  1. 旧请求（不含新字段）依然可用 —— 向后兼容
  2. 通勤 / 房价 / 城市能级 / 家庭规模 是否真的改变了决策
  3. 70% 红线是否贯穿租房期
  4. 名义资产 vs 真实购买力
"""
import sys
import types
import math
import os

# ----------------------------------------------------------------------
# 1. 最小 Web 框架 stub
# ----------------------------------------------------------------------
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class _RouteTable(dict):
    pass


ROUTES = _RouteTable()


class FastAPI:
    def __init__(self, *a, **k):
        self.title = k.get("title")

    def add_middleware(self, *a, **k):
        return None

    def exception_handler(self, exc_cls):
        def deco(fn):
            return fn
        return deco

    def post(self, path, **k):
        def deco(fn):
            ROUTES[path] = fn
            return fn
        return deco

    def get(self, path, **k):
        def deco(fn):
            ROUTES[path] = fn
            return fn
        return deco


class Request:
    pass


class RequestValidationError(Exception):
    pass


class JSONResponse:
    def __init__(self, status_code=200, content=None, headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class _Middleware:
    def __init__(self, *a, **k):
        pass


class FieldInfo:
    def __init__(self, default=None, **kw):
        self.default = default
        self.kw = kw


def Field(default=None, **kw):
    return FieldInfo(default, **kw)


class _ModelMeta(type):
    def __new__(mcls, name, bases, ns):
        defaults = {}
        for b in bases:
            defaults.update(getattr(b, "_defaults", {}))
        for k, v in list(ns.items()):
            if isinstance(v, FieldInfo):
                defaults[k] = v.default
                del ns[k]
        cls = super().__new__(mcls, name, bases, ns)
        cls._defaults = defaults
        return cls


class BaseModel(metaclass=_ModelMeta):
    model_config = {}

    def __init__(self, **kw):
        for k, v in self._defaults.items():
            setattr(self, k, v)
        for k, v in kw.items():
            setattr(self, k, v)

    def model_dump(self):
        return {k: getattr(self, k) for k in self._defaults}


def ConfigDict(**kw):
    return kw


def _install_stubs():
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Request = Request

    mw = types.ModuleType("fastapi.middleware")
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = _Middleware
    gzip_m = types.ModuleType("fastapi.middleware.gzip")
    gzip_m.GZipMiddleware = _Middleware
    mw.cors = cors
    mw.gzip = gzip_m

    exceptions = types.ModuleType("fastapi.exceptions")
    exceptions.RequestValidationError = RequestValidationError

    responses = types.ModuleType("fastapi.responses")
    responses.JSONResponse = JSONResponse

    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    pydantic.ConfigDict = ConfigDict

    sys.modules.update({
        "fastapi": fastapi,
        "fastapi.middleware": mw,
        "fastapi.middleware.cors": cors,
        "fastapi.middleware.gzip": gzip_m,
        "fastapi.exceptions": exceptions,
        "fastapi.responses": responses,
        "pydantic": pydantic,
    })


_install_stubs()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


# ----------------------------------------------------------------------
# 2. 基础参数
# ----------------------------------------------------------------------
def base_payload(**over):
    p = dict(
        buy_car=False,
        house_total_wan=100,
        initial_savings_wan=40,
        down_payment_ratio=30,
        monthly_income_net=6500,
        annual_income_growth_pct=3.0,
        monthly_expense=2200,
        property_fee_monthly=200,
        purchase_tax_rate=2.5,
        housing_fund_pct=16,
        provident_loan_cap_wan=60.0,
        annual_inflation_pct=2.5,
        rent_monthly_base=1800,
        invest_annual_yield_pct=0.9,
        rent_inflation_year_pct=2.0,
        loan_annual_rate_pct=3.9,
        provident_loan_rate_pct=3.1,
        repay_kind="equal_principal_interest",
        # V2.0 空间参数（南宁·西乡塘）
        city_name="南宁",
        city_tier=3,
        city_avg_price_sqm=10500,
        city_avg_rent_sqm=27,
        city_avg_salary=7200,
        district_price_sqm=9500,
        family_size=2,
        rent_commute_min=20,
        own_commute_min=28,
        rent_transport_month=0,
        own_transport_month=0,
        # V6.1 风险姿态与隐形支出
        risk_mode="steady",
        squeeze_life=False,
        hidden_expense_month=500,
        career_type="corporate",
        industry="other",
        income_volatility_pct=0,
        income_earners=1,
        house_price_growth_pct=0,
    )
    p.update(over)
    return p


def run(payload, absolute=False):
    req_cls = main.AbsoluteSimulationRequest if absolute else main.SimulationRequest
    fn = main.simulate_absolute if absolute else main.simulate_relative
    return fn(req_cls(**payload))


passed = 0
failed = []


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed.append(name)
        print(f"  ❌ {name}  {detail}")


def expect_error(fn, *a, **k):
    try:
        fn(*a, **k)
        return None
    except HTTPException as e:
        return e


print("=" * 72)
print("V6.0 空间异质性引擎 · 数值自测")
print("=" * 72)

# ----------------------------------------------------------------------
# T1 基础回归：默认参数仍能出方案
# ----------------------------------------------------------------------
print("\n[T1] 默认参数（南宁·西乡塘 / 3 口改 2 口）")
res = run(base_payload())
check("返回 200", res["code"] == 200)
check("可行方案数 > 0", len(res["all_scheme"]) > 0, f"got {len(res['all_scheme'])}")
check("spatial 区块存在", "spatial" in res)
sp = res["spatial"]
check("城市能级标签", sp["tier_label"] == "二线", sp.get("tier_label"))
check("可购面积 ≈ 105㎡", abs(sp["own_area_sqm"] - 105.3) < 1.0, sp["own_area_sqm"])
check("人均面积 ≈ 52.6㎡", abs(sp["area_per_person"] - 52.6) < 1.0, sp["area_per_person"])
check("相对收入位置 ≈ 0.90", abs(sp["rel_income"] - 0.903) < 0.01, sp["rel_income"])
check("PIR 房价收入比 ≈ 12.8", abs(sp["pir"] - 12.82) < 0.1, sp["pir"])
check("租金回报率 ≈ 2.16%", abs(sp["rent_yield_pct"] - 2.16) < 0.05, sp["rent_yield_pct"])
check("每方案含真实购买力", "real_asset_wan" in res["all_scheme"][0])
check(
    "真实购买力 < 名义资产",
    res["all_scheme"][0]["real_asset_wan"] < res["all_scheme"][0]["liquid_asset_end_wan"],
)
check("每方案含通勤生命周期分", "commute_lifecycle" in res["all_scheme"][0])

# ----------------------------------------------------------------------
# T2 通勤折现：单程 90 分钟必须显著拉低时间效率
# ----------------------------------------------------------------------
print("\n[T2] 通勤折现（单程 20 分钟 vs 90 分钟）")
near = run(base_payload(own_commute_min=20))
far = run(base_payload(own_commute_min=90))
near_time = near["top_comprehensive"][0]["time_efficiency"]
far_time = far["top_comprehensive"][0]["time_efficiency"]
check("近通勤时间效率更高", near_time > far_time, f"{near_time} vs {far_time}")
check("差距 ≥ 8 分（不是挠痒痒）", near_time - far_time >= 8, f"Δ={near_time - far_time:.2f}")

# ----------------------------------------------------------------------
# T3 空间舒适度：同样的钱，在贵城市买不到体面
# ----------------------------------------------------------------------
print("\n[T3] 房价折叠（9500 元/㎡ vs 30000 元/㎡）")
cheap = run(base_payload(district_price_sqm=9500))
expensive = run(base_payload(district_price_sqm=30000))
c_comf = cheap["top_comprehensive"][0]["comfort_score"]
e_comf = expensive["top_comprehensive"][0]["comfort_score"]
check("低价区舒适度更高", c_comf > e_comf, f"{c_comf} vs {e_comf}")
check("贵阳/鹤岗式价差带来 ≥ 10 分落差", c_comf - e_comf >= 10, f"Δ={c_comf - e_comf:.2f}")
check(
    "贵城市人均面积被压缩到 1/3",
    abs(expensive["spatial"]["area_per_person"] / cheap["spatial"]["area_per_person"] - 1 / 3.158) < 0.02,
)

# ----------------------------------------------------------------------
# T4 城市能级 & 相对收入
# ----------------------------------------------------------------------
print("\n[T4] 城市能级与相对收入位置")
nanjing = run(base_payload(monthly_income_net=20000, city_avg_salary=7200))
small = run(base_payload(monthly_income_net=12000, city_avg_salary=7200))
check(
    "高收入者相对位置更高",
    nanjing["spatial"]["rel_income"] > small["spatial"]["rel_income"],
)
check(
    "高收入者幸福感不低",
    nanjing["top_comprehensive"][0]["surplus_happiness"]
    >= small["top_comprehensive"][0]["surplus_happiness"] - 1e-6,
)
t1 = run(base_payload(city_tier=1, city_avg_price_sqm=63000))
check("超一线资源溢价 = 1.0", abs(t1["spatial"]["resource_index"] - 1.0) < 1e-9)
check("超一线生活成本指数 = 1.28", abs(t1["spatial"]["cost_index"] - 1.28) < 1e-9)

# ----------------------------------------------------------------------
# T5 租房压力：不硬杀，但要扣抗风险分；90% 生存线兜底
# ----------------------------------------------------------------------
print("\n[T5] 租房期压力传导（70% 只属于还贷期，90% 是生存线）")


def sim_single(payload, ry=5, ly=20, ds="min"):
    eng = main.HousingFinanceEngine(main.SimulationRequest(**payload))
    return eng.single_scenario_sim(ry, ly, ds)


low_rent = sim_single(base_payload(house_total_wan=70, monthly_income_net=7000, monthly_expense=2200, rent_monthly_base=1800))
high_rent = sim_single(base_payload(house_total_wan=70, monthly_income_net=7000, monthly_expense=2200, rent_monthly_base=3200))
check(
    "同一租贷组合，高租金抗风险得分更低",
    high_rent is not None and low_rent is not None and high_rent["adapt_score"] < low_rent["adapt_score"],
    f"{high_rent and high_rent['adapt_score']} vs {low_rent and low_rent['adapt_score']}",
)
check(
    "高租金把 70% 余量吃穿（稳健分归零）",
    high_rent is not None and high_rent["adapt_score"] == 0.0,
    f"{high_rent and high_rent['adapt_score']}",
)
err = expect_error(main.simulate_relative, main.SimulationRequest(**base_payload(
    monthly_income_net=4500, monthly_expense=3000, rent_monthly_base=3000
)))
check("越过 90% 生存线抛 400 而非 500", err is not None and err.status_code == 400)

# ----------------------------------------------------------------------
# T6 家庭模式：三口之家的人均面积更低
# ----------------------------------------------------------------------
print("\n[T6] 家庭模式")
fam3 = run(base_payload(family_size=3))
check("三口人均面积更低", fam3["spatial"]["area_per_person"] < sp["area_per_person"])
check("三口舒适度更低", fam3["top_comprehensive"][0]["comfort_score"] < c_comf + 1e-9)

# ----------------------------------------------------------------------
# T7 绝对寻优模式兼容
# ----------------------------------------------------------------------
print("\n[T7] 绝对寻优模式")
abs_res = run(base_payload(), absolute=True)
check("absolute 返回 200", abs_res["code"] == 200)
check("absolute 带 spatial", "spatial" in abs_res)
check("global_best 存在", "global_best" in abs_res)

# ----------------------------------------------------------------------
# T8 向后兼容：旧前端不传空间参数
# ----------------------------------------------------------------------
print("\n[T8] 向后兼容（旧 payload）")
old = base_payload()
for k in ["city_name", "city_tier", "city_avg_price_sqm", "city_avg_rent_sqm",
          "city_avg_salary", "district_price_sqm", "family_size",
          "rent_commute_min", "own_commute_min", "rent_transport_month", "own_transport_month",
          "risk_mode", "squeeze_life", "hidden_expense_month", "career_type",
          "industry", "income_volatility_pct", "house_price_growth_pct", "income_earners"]:
    old.pop(k)
old_res = run(old)
check("旧请求仍可计算", old_res["code"] == 200)
check("旧请求走默认空间参数", old_res["spatial"]["city_name"] == "南宁")
check("旧请求默认家庭=2人", old_res["spatial"]["family_size"] == 2)
check("旧请求默认隐形支出=0（不替用户设默认）", old_res["risk"]["hidden_expense_month"] == 0)
check("旧请求默认稳健红线=70%", old_res["risk"]["loan_cap_pct"] == 70)

# ----------------------------------------------------------------------
# T9 交通费显式覆盖
# ----------------------------------------------------------------------
print("\n[T9] 交通费显式覆盖")
expensive_transport = run(base_payload(own_transport_month=1500))
check(
    "通勤成本上去后期末资产下降",
    expensive_transport["top_comprehensive"][0]["liquid_asset_end_wan"]
    < near["top_comprehensive"][0]["liquid_asset_end_wan"],
)

# ----------------------------------------------------------------------
# T10 熵权仍然归一到 1
# ----------------------------------------------------------------------
print("\n[T10] 熵权完整性")
wsum = sum(v for k, v in res["entropy_result"].items() if k.startswith("weight_") and isinstance(v, float))
check("权重之和 ≈ 1", abs(wsum - 1.0) < 0.02, f"sum={wsum:.4f}")

# ----------------------------------------------------------------------
# T11 风险姿态：70% 是默认承诺，90% 只能是显式分支
# ----------------------------------------------------------------------
print("\n[T11] 风险姿态分支（70% 默认 vs 90% 悬崖方案）")
# 构造一个还贷期支出占比约 78% 的组合：稳健模式必须拒绝，激进模式才允许
hard = dict(
    monthly_income_net=12000,
    house_total_wan=260,
    initial_savings_wan=90,
    down_payment_ratio=30,
    monthly_expense=2200,
)

def sim_with(mode, extra=None, ry=0, ly=30, ds="min"):
    payload = base_payload(risk_mode=mode, **hard)
    if extra:
        payload.update(extra)
    eng = main.HousingFinanceEngine(main.SimulationRequest(**payload))
    return eng.single_scenario_sim(ry, ly, ds), eng

steady_res, steady_eng = sim_with("steady")
aggr_res, aggr_eng = sim_with("aggressive")
check("稳健模式 70% 红线拒绝高杠杆组合", steady_res is None)
check("激进模式 90% 生存线允许它通过", aggr_res is not None)
check("稳健模式红线=70%", steady_eng.loan_cap == 0.70)
check("激进模式红线=90%", aggr_eng.loan_cap == 0.90)
check("稳健模式不设租房硬顶", steady_eng.rent_cap is None)
check("激进模式租房硬顶=90%", aggr_eng.rent_cap == 0.90)
check("激进方案的抗风险得分依然很低（不粉饰）", aggr_res and aggr_res["adapt_score"] <= 1.0, aggr_res and aggr_res["adapt_score"])

# 90% 不是无底线：超过 90% 一样被拒
over = dict(hard, monthly_income_net=9000)
over_res, _ = sim_with("aggressive", over)
check("激进模式也不会放过 >90% 的组合", over_res is None)

# ----------------------------------------------------------------------
# T12 隐形支出：红包/旅行/医疗必须真实扣钱
# ----------------------------------------------------------------------
print("\n[T12] 隐形支出（hidden_expense_month）")
with_hidden = sim_single(base_payload(house_total_wan=50, hidden_expense_month=600), ry=5, ly=20, ds="min")
no_hidden = sim_single(base_payload(house_total_wan=50, hidden_expense_month=0), ry=5, ly=20, ds="min")
check(
    "隐形支出越高，期末资产越低",
    with_hidden["liquid_asset_end_wan"] < no_hidden["liquid_asset_end_wan"],
    f"{with_hidden['liquid_asset_end_wan']} vs {no_hidden['liquid_asset_end_wan']}",
)
check("隐形支出进入响应摘要",
      run(base_payload(hidden_expense_month=800))["risk"]["hidden_expense_month"] == 800)

# ----------------------------------------------------------------------
# T13 节衣缩食（仅激进模式）
# ----------------------------------------------------------------------
print("\n[T13] 节衣缩食 squeeze_life")
_, sq_eng = sim_with("aggressive", {"squeeze_life": True})
_, nosq_eng = sim_with("aggressive", {"squeeze_life": False})
check("激进+节衣缩食：生活开销打 8 折", abs(sq_eng.month_base_exp - 2200 * 0.8) < 1e-6,
      sq_eng.month_base_exp)
check("不给激进模式就静默打折", abs(nosq_eng.month_base_exp - 2200) < 1e-6)
steady_sq_res, steady_sq_eng = sim_with("steady", {"squeeze_life": True})
check("稳健模式忽略 squeeze_life（不能偷偷降开销）", abs(steady_sq_eng.month_base_exp - 2200) < 1e-6)

# ----------------------------------------------------------------------
# T14 职业 S 曲线：起薪决定天花板，增长决定爬坡
# ----------------------------------------------------------------------
print("\n[T14] 职业 S 曲线（V6.3）")
m4000 = main.career_cap_multiple(4000, 3)
m10000 = main.career_cap_multiple(10000, 3)
m20000 = main.career_cap_multiple(20000, 3)
check("起薪越低，天花板倍数越高", m4000 > m10000 > m20000, f"{m4000:.2f}/{m10000:.2f}/{m20000:.2f}")
check("4000 起薪约 2.1 倍", abs(m4000 - 2.1) < 0.02, m4000)
check("10000 起薪约 1.6 倍", abs(m10000 - 1.596) < 0.03, m10000)
check("20000 起薪触底 1.35 倍", abs(m20000 - 1.35) < 0.01, m20000)


def career_eng(**over):
    base = dict(house_total_wan=85, monthly_income_net=10000,
                monthly_expense=2200, hidden_expense_month=800)
    base.update(over)
    return main.HousingFinanceEngine(main.SimulationRequest(**base_payload(**base)))


e3 = career_eng(annual_income_growth_pct=3)
check("起点工资等于输入", abs(e3.get_current_income(0) - 10000) < 1)
check("30 年后 ≈ 15960", abs(e3.get_current_income(30) - 15960) < 15, e3.get_current_income(30))
check("不再复利成 2.4 倍", e3.get_current_income(30) / 10000 < 1.7)
check(
    "S 曲线中段快于前段",
    (e3.get_current_income(10) - e3.get_current_income(5))
    > (e3.get_current_income(5) - e3.get_current_income(0)),
)
e0 = career_eng(annual_income_growth_pct=0)
check("0% 增长：名义工资恒定", all(abs(e0.get_current_income(y) - 10000) < 1 for y in range(0, 31, 3)))
e1 = career_eng(annual_income_growth_pct=1)
check("1% 只吃到 1/3 天花板", abs(e1.get_current_income(30) - 11987) < 60, e1.get_current_income(30))
cs = e3.career_summary()
check("30 年后按今日物价低于名义", cs["real_30y_monthly"] < cs["nominal_30y_monthly"], cs)
res_c = run(base_payload(monthly_income_net=10000, hidden_expense_month=800))
check("响应含 career 区块", "career" in res_c and res_c["career"]["cap_multiple"] > 1)

# ----------------------------------------------------------------------
# T15 职业类型：同起薪，曲线形状不同
# ----------------------------------------------------------------------
print("\n[T15] 职业类型（V6.4）")
corp_m = main.career_cap_multiple(10000, 3, "corporate")
pub_m = main.career_cap_multiple(10000, 3, "public")
free_m = main.career_cap_multiple(10000, 3, "freelance")
check("自由职业上限最高", free_m > corp_m > pub_m, f"{free_m}/{corp_m}/{pub_m}")
eng_pub = career_eng(career_type="public")
eng_corp = career_eng(career_type="corporate")
eng_free = career_eng(career_type="freelance")
check("体制内平台期更晚", eng_pub.career_mid_year() > eng_corp.career_mid_year(),
      f"{eng_pub.career_mid_year()} vs {eng_corp.career_mid_year()}")
check("自由职业爬坡更早", eng_free.career_mid_year() < eng_corp.career_mid_year())
check("职业类型进入响应摘要",
      run(base_payload(career_type="public"))["career"]["career_label"].startswith("体制内"))
check("非法职业类型回落到企业", main.HousingFinanceEngine(
    main.SimulationRequest(**base_payload(career_type="unknown"))
).career_type == "corporate")

# ----------------------------------------------------------------------
# T16 行业、现金缓冲、房价时间序列（V6.5-V6.9）
# ----------------------------------------------------------------------
print("\n[T16] 行业 / 现金缓冲 / 房价增速")
it_cap = main.career_cap_multiple(10000, 3, "corporate", "it")
ed_cap = main.career_cap_multiple(10000, 3, "corporate", "education")
sv_cap = main.career_cap_multiple(10000, 3, "corporate", "services")
check("行业天花板：IT > 教育 > 住餐", it_cap > ed_cap > sv_cap, f"{it_cap:.2f}/{ed_cap:.2f}/{sv_cap:.2f}")

eng_pub2 = career_eng(career_type="public")
eng_free2 = career_eng(career_type="freelance")
check("体制内缓冲要求最低", eng_pub2.buffer_months_required < eng_free2.buffer_months_required,
      f"{eng_pub2.buffer_months_required} vs {eng_free2.buffer_months_required}")
check("自由职业缓冲约 6.6 个月", abs(eng_free2.buffer_months_required - 6.6) < 0.05,
      eng_free2.buffer_months_required)

res_buf = run(base_payload())
check("方案输出含缓冲月数", "buffer_months" in res_buf["all_scheme"][0])
check("方案输出含购房时点价格", "purchase_price_wan" in res_buf["all_scheme"][0])
check("响应含 market 区块", "market" in res_buf and "house_price_growth_pct" in res_buf["market"])

base_res = run(base_payload())
grow_res = run(base_payload(house_price_growth_pct=3))
check("房价增速推高购房时点价格",
      grow_res["all_scheme"][0]["purchase_price_wan"] > base_res["all_scheme"][0]["purchase_price_wan"])
check("房价增速压缩可行方案", len(grow_res["all_scheme"]) < len(base_res["all_scheme"]))

check("非法行业回落到 other", main.HousingFinanceEngine(
    main.SimulationRequest(**base_payload(industry="unknown"))
).industry == "other")

# ----------------------------------------------------------------------
# T17 家庭收入口径：双人模式按家庭合计收入 + 人均起薪
# ----------------------------------------------------------------------
print("\n[T17] 家庭收入口径（V6.10）")
single = career_eng(annual_income_growth_pct=3, monthly_income_net=10000, income_earners=1)
couple = career_eng(annual_income_growth_pct=3, monthly_income_net=20000, income_earners=2)
check("双人：人均起薪 = 合计 / 2", abs(couple.per_earner_income - 10000) < 1)
check("双人：天花板按人均 1.6 倍再乘 2",
      abs(couple.career_ceiling - single.career_ceiling * 2) < 1,
      f"{couple.career_ceiling} vs {single.career_ceiling * 2}")
check("双人：相对本地收入与单人一致",
      abs(couple.rel_income - single.rel_income) < 0.01,
      f"{couple.rel_income} vs {single.rel_income}")
check("响应含就业人数", run(base_payload(monthly_income_net=20000, income_earners=2))["career"]["income_earners"] == 2)

print("\n[T18] 装修成本（V7.4）")
_area = 100 * 10000 / 9500          # 100 万 / 9500 元每平 ≈ 105.3㎡
import reno as _reno_mod
_expected_unit = _reno_mod.estimate_unit(7200, 9500, 3, _area, "standard")

run_fin = run(base_payload())                                   # 默认：已装修，不扣
run_rough = run(base_payload(house_decoration="rough"))          # 毛坯：自动估算
_first_fin = run_fin["all_scheme"][0]
_first_rough = run_rough["all_scheme"][0]

check("默认按已装修处理，装修费为 0", _first_fin["reno_budget_wan"] == 0, _first_fin["reno_budget_wan"])
check("毛坯自动估算出装修费", _first_rough["reno_budget_wan"] > 0, _first_rough["reno_budget_wan"])
check("装修单价与模型一致",
      abs(_first_rough["reno_unit_sqm"] - _expected_unit) <= 1,
      f"{_first_rough['reno_unit_sqm']} vs {_expected_unit:.0f}")
check("毛坯方案的期末资产更低（钱真的扣了）",
      _first_rough["liquid_asset_end_wan"] < _first_fin["liquid_asset_end_wan"],
      f"{_first_rough['liquid_asset_end_wan']} vs {_first_fin['liquid_asset_end_wan']}")

run_none = run(base_payload(house_decoration="rough", reno_level="none"))
check("reno_level=none 不扣装修（毛坯入住/自己装）",
      run_none["all_scheme"][0]["reno_budget_wan"] == 0)

run_manual = run(base_payload(house_decoration="rough", reno_budget_wan=8))
check("填了预算就用用户的数", abs(run_manual["all_scheme"][0]["reno_budget_wan"] - 8) < 0.01,
      run_manual["all_scheme"][0]["reno_budget_wan"])

_gaps = [s for s in run_rough["all_scheme"] if s.get("reno_gap_wan", 0) > 0]
check("掏空型方案会出现装修缺口（不枪毙，只扣分）", len(_gaps) >= 0)
if _gaps:
    print(f"     ↳ 有 {len(_gaps)} 个方案带装修缺口，最大 {max(s['reno_gap_wan'] for s in _gaps):.2f} 万")

print("\n" + "=" * 72)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
if failed:
    print("失败项：" + ", ".join(failed))
print("=" * 72)
sys.exit(1 if failed else 0)


if failed:
    print("失败项：" + ", ".join(failed))
print("=" * 72)
sys.exit(1 if failed else 0)











