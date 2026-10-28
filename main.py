from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict
import math
import logging
import time
from typing import List, Dict, Optional

# 日志配置
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)

# =====================================================================
# 🌐 全局常量与配置
# =====================================================================
SIM_TOTAL_YEARS = 30
RENT_CANDIDATES = list(range(0, 21))  # 完整保留 0 到 20 年
LOAN_CANDIDATES = list(range(5, 31))  # 完整保留 5 到 30 年
RENT_LEVELS_FOR_ABSOLUTE = [1200, 1500, 1800, 2200, 2600, 3000, 3500, 4000]
DOWN_STRATEGIES = ["min", "balanced", "max"]  # 新增：首付策略（最低、折中、倾尽全力）

TOP_N = 20
PER_GROUP_N = 3
STYLE_TOP_N = 5
EPS_YUAN = 0.01
EPS_FLOAT = 1e-9
EPS_LOG = 1e-12

DEFICIT_REF_MONTH = 6
RENT_SALE_RATIO = 0.03

SUITABILITY_WEIGHTS = {"comfort": 0.30, "adapt": 0.30, "happiness": 0.25, "time": 0.15}

ENTROPY_INDICATORS = [
    ("asset", "liquid_asset_end_wan"),
    ("adapt", "adapt_score"),
    ("comfort", "comfort_score"),
    ("happiness", "surplus_happiness"),
    ("time", "time_efficiency"),
]

MODEL_ASSUMPTIONS = [
    "流动资产仅计现金理财余额，不含房产残值、契税、维修基金等交易税费（坚决拒绝纸面财富幻觉）",
    "公积金缴存基数简化等同于输入的月收入；housing_fund_pct为个人+单位合计缴存比例，双边上限24%",
    "租房阶段：公积金按月提取，计入当月可支配现金流；购房还贷阶段，公积金按月对冲抵扣月供",
    "租金按年度复利调价，同一年内12个月租金固定不变",
    "严苛财务红线：无论何种方案，购房还贷期（真实月供+生活开销+物业费）超过月收入 70% 的组合将被直接一票否决剔除",
    "小康家庭定义：全生命周期平均月度纯现金结余必须达到或超过总收入的 50%，抗拒沦为被账单绑架的打工机器",
    "反资本剥削：贷款年限越短、首付比例越高，时间效率得分越高，鼓励尽早赎清贷款",
    "抗风险稳健度算法升级：基于生命周期中最紧绷月份的“真实支出占比”距离70%红线的安全余量来计算",
    "首付策略升级：输入的首付比例仅作为最低门槛，系统会智能遍历最低首付、折中首付、极限高首付（掏空现金）策略",
    "30年周期内未结清贷款的组合直接剔除，不跨周期挂账",
    "绝对模式采用离散租金档位抽样遍历，保留全部0至20年租期",
]

NANNING_REGIONS = [
    {
        "name": "青秀区",
        "lat": 22.79,
        "lng": 108.38,
        "house_ref": 140,
        "rent_ref": 3000,
        "desc": "核心城区，配套完善，名校云集",
    },
    {
        "name": "西乡塘区",
        "lat": 22.84,
        "lng": 108.28,
        "house_ref": 85,
        "rent_ref": 1600,
        "desc": "科教片区，高校林立，生活成本低",
    },
    {
        "name": "江南区",
        "lat": 22.78,
        "lng": 108.28,
        "house_ref": 78,
        "rent_ref": 1500,
        "desc": "老城区，刚需友好，烟火气浓",
    },
    {
        "name": "兴宁区",
        "lat": 22.85,
        "lng": 108.33,
        "house_ref": 92,
        "rent_ref": 1900,
        "desc": "老城商圈，火车站附近，交通便利",
    },
    {
        "name": "良庆区",
        "lat": 22.74,
        "lng": 108.36,
        "house_ref": 110,
        "rent_ref": 2300,
        "desc": "五象新区核心地带，发展潜力大",
    },
    {
        "name": "邕宁区",
        "lat": 22.76,
        "lng": 108.48,
        "house_ref": 82,
        "rent_ref": 1600,
        "desc": "五象东扩区，价格洼地，生态宜居",
    },
    {
        "name": "武鸣区",
        "lat": 23.16,
        "lng": 108.27,
        "house_ref": 55,
        "rent_ref": 1200,
        "desc": "教育园区，大学城，价格极低",
    },
    {
        "name": "五象新区",
        "lat": 22.72,
        "lng": 108.35,
        "house_ref": 120,
        "rent_ref": 2500,
        "desc": "自治区级重点发展新区，总部基地",
    },
]

LOAN_SCHEDULE_CACHE: Dict[tuple, List[float]] = {}


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 汽车变量
    buy_car: bool = Field(default=False)
    car_buy_year: int = Field(
        ge=0, default=0
    )  # 🌟 新增：计划购车年份（0代表现在，5代表第5年）
    car_total_wan: float = Field(ge=0, default=0)
    car_loan_year: int = Field(ge=0, default=0)
    car_monthly_expense: float = Field(ge=0, default=0)

    # 基础资产
    house_total_wan: float = Field(gt=0)
    initial_savings_wan: float = Field(ge=0)
    down_payment_ratio: float = Field(ge=15, le=80)

    # 收入与开销
    monthly_income_net: float = Field(gt=0)
    annual_income_growth_pct: float = Field(ge=0, default=3.0)
    monthly_expense: float = Field(gt=0)
    property_fee_monthly: float = Field(ge=0)

    # 购房摩擦成本
    purchase_tax_rate: float = Field(ge=0, default=2.5)


    # 金融假设
    housing_fund_pct: float = Field(ge=0, le=24, default=16)
    provident_loan_cap_wan: float = Field(ge=0, default=60.0)

    rent_monthly_base: float = Field(gt=0, default=1800)
    invest_annual_yield_pct: float = Field(ge=0)
    rent_inflation_year_pct: float = Field(ge=0)

    loan_annual_rate_pct: float = Field(gt=0)
    provident_loan_rate_pct: float = Field(gt=0, default=3.1)

    repay_kind: str = Field(pattern="^(equal_principal_interest|equal_principal)$")


class AbsoluteSimulationRequest(SimulationRequest):
    pass


# =====================================================================
# 🏦 仿真引擎
# =====================================================================
class HousingFinanceEngine:
    def __init__(self, req: SimulationRequest):
        self.req = req
        self.house_total_yuan = req.house_total_wan * 10000
        self.init_saving = req.initial_savings_wan * 10000

        self.min_down_ratio = req.down_payment_ratio / 100.0
        self.min_down_payment_yuan = self.house_total_yuan * self.min_down_ratio

        self.base_income = req.monthly_income_net
        self.income_growth_rate = req.annual_income_growth_pct / 100.0  # 动态收入

        self.month_base_exp = req.monthly_expense
        self.month_property = req.property_fee_monthly
        self.month_fund_inflow = self.base_income * (req.housing_fund_pct / 100.0)

        # 🌟 汽车相关（修复报错！）
        self.buy_car = req.buy_car
        self.car_buy_year = req.car_buy_year  # 补上这一行，解决 500 报错
        self.car_total_yuan = req.car_total_wan * 10000
        self.car_loan_months = req.car_loan_year * 12
        self.car_monthly_expense = req.car_monthly_expense
        self.car_loan_rate = 0.05 / 12.0

        self.base_rent = req.rent_monthly_base
        self.invest_year_rate = req.invest_annual_yield_pct / 100.0
        self.month_invest_rate = (1 + self.invest_year_rate) ** (1 / 12) - 1
        self.rent_inflation = req.rent_inflation_year_pct / 100.0

        self.loan_year_rate = req.loan_annual_rate_pct / 100.0
        self.prov_loan_rate = req.provident_loan_rate_pct / 100.0
        self.prov_cap_yuan = req.provident_loan_cap_wan * 10000
        self.repay_kind = req.repay_kind
        self.purchase_tax_rate = req.purchase_tax_rate / 100.0

    def set_base_rent(self, new_rent: float):
        self.base_rent = new_rent

    # 🌟 新增：核心薪资增长算法（分段式）
    def get_current_income(self, year_idx: int) -> float:
        """
        重构薪资增长：
        0-5年：按设定的增长率 (如 5%)
        6-15年：增长率减半 (如 2.5%)
        15年以后：几乎停滞 (0.5%)
        """
        if year_idx <= 5:
            return self.base_income * ((1 + self.income_growth_rate) ** year_idx)
        elif year_idx <= 15:
            base_5 = self.base_income * ((1 + self.income_growth_rate) ** 5)
            return base_5 * ((1 + self.income_growth_rate / 2) ** (year_idx - 5))
        else:
            base_15 = self.base_income * ((1 + self.income_growth_rate) ** 5) * ((1 + self.income_growth_rate / 2) ** 10)
            return base_15 * (1.005 ** (year_idx - 15)) # 15年后象征性涨 0.5%

    def build_combo_loan_schedule(self, loan_year: int, total_loan_principal_yuan: float) -> List[float]:
        if total_loan_principal_yuan <= EPS_YUAN:
            return [0.0] * (loan_year * 12)

        prov_loan_yuan = min(total_loan_principal_yuan, self.prov_cap_yuan)
        comm_loan_yuan = total_loan_principal_yuan - prov_loan_yuan
        n = loan_year * 12

        def calc_schedule(principal, year_rate):
            if principal <= EPS_YUAN: return [0.0] * n
            month_rate = year_rate / 12.0
            if self.repay_kind == "equal_principal_interest":
                m = principal * month_rate * (1 + month_rate) ** n / ((1 + month_rate) ** n - 1)
                return [m] * n
            else:
                principal_m = principal / n
                remain = principal
                sch = []
                for _ in range(n):
                    sch.append(principal_m + remain * month_rate)
                    remain -= principal_m
                return sch

        prov_schedule = calc_schedule(prov_loan_yuan, self.prov_loan_rate)
        comm_schedule = calc_schedule(comm_loan_yuan, self.loan_year_rate)
        return [prov_schedule[i] + comm_schedule[i] for i in range(n)]

    def single_scenario_sim(self, rent_year: int, loan_year: int, down_strategy: str) -> Optional[Dict]:
        if loan_year > (SIM_TOTAL_YEARS - rent_year): return None

        rent_month = rent_year * 12
        loan_month = loan_year * 12
        remain_cash = self.init_saving

        car_down = 0.0
        car_monthly_mortgage = 0.0
        if self.buy_car:
            if self.car_loan_months == 0:
                car_down = self.car_total_yuan
            else:
                car_down = self.car_total_yuan * 0.3
            if self.car_loan_months > 0:
                car_loan_principal = self.car_total_yuan * 0.7
                car_monthly_mortgage = (car_loan_principal * self.car_loan_rate * (1 + self.car_loan_rate) ** self.car_loan_months / ((1 + self.car_loan_rate) ** self.car_loan_months - 1))

        car_buy_month_idx = self.car_buy_year * 12 

        def get_car_cost(current_idx):
            cost = 0.0
            if not self.buy_car: return cost
            if current_idx >= car_buy_month_idx:
                cost += self.car_monthly_expense
                if current_idx < car_buy_month_idx + self.car_loan_months:
                    cost += car_monthly_mortgage
            return cost

        max_deficit = 0.0
        total_months_surplus_rate = 0.0
        max_outflow_ratio = 0.0

        # 阶段1：租房攒首付
        for t in range(rent_month):
            if self.buy_car and t == car_buy_month_idx:
                if remain_cash < car_down: return None
                remain_cash -= car_down

            year_idx = t // 12
            current_income = self.get_current_income(year_idx) # 🌟 调用新算法
            current_fund = current_income * (self.req.housing_fund_pct / 100.0)
            current_rent = self.base_rent * ((1 + self.rent_inflation) ** year_idx)

            car_cost = get_car_cost(t)
            real_monthly_outflow = self.month_base_exp + current_rent + car_cost
            ratio = real_monthly_outflow / current_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio

            monthly_net = (current_income + current_fund) - real_monthly_outflow
            total_months_surplus_rate += monthly_net / current_income
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: return None
            remain_cash = max(0.0, remain_cash)

        purchase_tax_yuan = self.house_total_yuan * self.purchase_tax_rate
        total_upfront_needed = self.min_down_payment_yuan + purchase_tax_yuan
        if remain_cash < total_upfront_needed - EPS_YUAN: return None

        actual_down_payment_yuan = 0.0
        if down_strategy == "min":
            actual_down_payment_yuan = self.min_down_payment_yuan
        elif down_strategy == "max":
            actual_down_payment_yuan = min(remain_cash - purchase_tax_yuan, self.house_total_yuan)
        elif down_strategy == "balanced":
            extra = (remain_cash - total_upfront_needed) * 0.5
            actual_down_payment_yuan = min(self.min_down_payment_yuan + extra, self.house_total_yuan)

        actual_loan_principal = self.house_total_yuan - actual_down_payment_yuan
        remain_cash -= actual_down_payment_yuan + purchase_tax_yuan
        remain_cash = max(0.0, remain_cash)

        # 阶段2：还贷期
        loan_schedule = self.build_combo_loan_schedule(loan_year, actual_loan_principal)
        for m in range(loan_month):
            current_idx = rent_month + m
            if self.buy_car and current_idx == car_buy_month_idx:
                if remain_cash < car_down: return None
                remain_cash -= car_down

            year_idx = rent_year + (m // 12)
            current_income = self.get_current_income(year_idx) # 🌟 调用新算法
            current_fund = current_income * (self.req.housing_fund_pct / 100.0)

            fund_deduct = min(current_fund, loan_schedule[m])
            actual_out_of_pocket = loan_schedule[m] - fund_deduct
            car_cost = get_car_cost(current_idx)
            
            real_monthly_outflow = self.month_base_exp + self.month_property + actual_out_of_pocket + car_cost
            ratio = real_monthly_outflow / current_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio
            if real_monthly_outflow > (current_income * 0.70): return None

            monthly_net = current_income - real_monthly_outflow
            total_months_surplus_rate += monthly_net / current_income
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: return None
            remain_cash = max(0.0, remain_cash)

        # 阶段3：自由期
        rest_month = SIM_TOTAL_YEARS * 12 - rent_month - loan_month
        for m in range(rest_month):
            current_idx = rent_month + loan_month + m
            if self.buy_car and current_idx == car_buy_month_idx:
                if remain_cash < car_down: return None
                remain_cash -= car_down

            year_idx = rent_year + loan_year + (m // 12)
            current_income = self.get_current_income(year_idx) # 🌟 调用新算法
            car_cost = get_car_cost(current_idx)
            
            real_monthly_outflow = self.month_base_exp + self.month_property + car_cost
            ratio = real_monthly_outflow / current_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio

            monthly_net = current_income - real_monthly_outflow
            total_months_surplus_rate += monthly_net / current_income
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            remain_cash = max(0.0, remain_cash)

        safety_margin = 0.70 - max_outflow_ratio
        adapt_score = min(100.0, max(0.0, (safety_margin / 0.40) * 100))

        implied_rent_house = (self.base_rent * 12 / RENT_SALE_RATIO) / 10000
        target_house = self.req.house_total_wan
        rent_comfort = 82.0 if implied_rent_house >= target_house else 60.0 + 22.0 * (implied_rent_house / target_house)
        buy_comfort = 88.0
        occupied_years = SIM_TOTAL_YEARS - rent_year
        comfort_score = (rent_year * rent_comfort + occupied_years * buy_comfort) / SIM_TOTAL_YEARS

        avg_surplus_rate = total_months_surplus_rate / (SIM_TOTAL_YEARS * 12)
        happiness = 100.0 if avg_surplus_rate >= 0.50 else max(0.0, (avg_surplus_rate / 0.50) * 100)

        time_eff_raw = max(0.0, 100.0 - (rent_year / SIM_TOTAL_YEARS * 30 + loan_year / SIM_TOTAL_YEARS * 70))
        time_eff = 60.0 + time_eff_raw * 0.4

        return {
            "rent_year": rent_year, "loan_year": loan_year, "down_strategy": down_strategy,
            "occupied_years": round(occupied_years, 2),
            "down_payment_wan": round(actual_down_payment_yuan / 10000, 2),
            "loan_principal_wan": round(actual_loan_principal / 10000, 2),
            "liquid_asset_end_wan": round(remain_cash / 10000, 2),
            "max_deficit_wan": round(max_deficit / 10000, 2),
            "risk_score": round(100.0 - adapt_score, 2),
            "adapt_score": round(adapt_score, 2),
            "comfort_score": round(comfort_score, 2),
            "surplus_happiness": round(happiness, 2),
            "time_efficiency": round(time_eff, 2),
        }

# =====================================================================
# 📊 熵权与综合得分计算
# =====================================================================
def entropy_weight_multi(scheme_list: List[Dict]) -> Dict[str, float]:
    n = len(scheme_list)
    if n <= 1:
        w = 1.0 / len(ENTROPY_INDICATORS)
        return {f"weight_{k}": round(w, 4) for k, _ in ENTROPY_INDICATORS} | {
            f"entropy_{k}": 0.0 for k, _ in ENTROPY_INDICATORS
        }

    ew = {}
    d_list = []
    for short, field in ENTROPY_INDICATORS:
        vec = [s[field] for s in scheme_list]
        min_v, max_v = min(vec), max(vec)
        if max_v - min_v < EPS_FLOAT:
            norm = [1.0 / n for _ in vec]
        else:
            norm = [
                max(EPS_LOG, min(1 - EPS_LOG, (v - min_v) / (max_v - min_v)))
                for v in vec
            ]
        p = [x / sum(norm) if sum(norm) > 0 else 0 for x in norm]
        e = -sum(x * math.log(x) for x in p if x > EPS_LOG) / math.log(n)
        ew[f"entropy_{short}"] = round(e, 4)
        d_list.append(1 - e)

    sum_d = sum(d_list)
    for idx, (short, _) in enumerate(ENTROPY_INDICATORS):
        w = d_list[idx] / sum_d if sum_d > EPS_FLOAT else 1.0 / len(ENTROPY_INDICATORS)
        ew[f"weight_{short}"] = round(w, 4)

    w_sum = sum(ew[f"weight_{k}"] for k, _ in ENTROPY_INDICATORS)
    ew["weight_warning"] = (
        f"权重之和异常({w_sum:.3f})" if not (0.98 <= w_sum <= 1.02) else None
    )
    return ew


def attach_comprehensive(scheme_list: List[Dict], ew: Dict) -> List[Dict]:
    if not scheme_list:
        return []

    if len(scheme_list) == 1:
        item = scheme_list[0]
        item["comprehensive_score"] = 100.0
        item["suitability_score"] = round(
            item["comfort_score"] * SUITABILITY_WEIGHTS["comfort"]
            + item["adapt_score"] * SUITABILITY_WEIGHTS["adapt"]
            + item["surplus_happiness"] * SUITABILITY_WEIGHTS["happiness"]
            + item["time_efficiency"] * SUITABILITY_WEIGHTS["time"],
            2,
        )
        return scheme_list

    mins = {k: min(s[f] for s in scheme_list) for k, f in ENTROPY_INDICATORS}
    maxs = {k: max(s[f] for s in scheme_list) for k, f in ENTROPY_INDICATORS}

    for item in scheme_list:
        comp = 0.0
        for k, f in ENTROPY_INDICATORS:
            if maxs[k] - mins[k] < EPS_FLOAT:
                norm_val = 0.0
            else:
                norm_val = (item[f] - mins[k]) / (maxs[k] - mins[k])
            comp += norm_val * ew[f"weight_{k}"]

        item["comprehensive_score"] = round(comp * 100, 2)
        item["suitability_score"] = round(
            item["comfort_score"] * SUITABILITY_WEIGHTS["comfort"]
            + item["adapt_score"] * SUITABILITY_WEIGHTS["adapt"]
            + item["surplus_happiness"] * SUITABILITY_WEIGHTS["happiness"]
            + item["time_efficiency"] * SUITABILITY_WEIGHTS["time"],
            2,
        )
    return sorted(scheme_list, key=lambda x: x["comprehensive_score"], reverse=True)


def build_diversified_by_rent(out_list: List[Dict]) -> Dict[str, List[Dict]]:
    groups: Dict[str, List[Dict]] = {}
    for item in out_list:
        k = str(item["rent_year"])
        groups.setdefault(k, [])
        if len(groups[k]) < PER_GROUP_N:
            groups[k].append(item)
    return groups


def build_style_boards(out_list: List[Dict]) -> Dict[str, List[Dict]]:
    # 将首付策略（进取/稳健）自然地映射到风格看板中
    return {
        "稳健型": sorted(
            [x for x in out_list if x["down_strategy"] in ("max", "balanced")],
            key=lambda x: x["adapt_score"],
            reverse=True,
        )[:STYLE_TOP_N],
        "平衡型": sorted(
            [x for x in out_list if x["down_strategy"] == "balanced"],
            key=lambda x: x["suitability_score"],
            reverse=True,
        )[:STYLE_TOP_N],
        "进取型": sorted(
            [x for x in out_list if x["down_strategy"] == "min"],
            key=lambda x: x["liquid_asset_end_wan"],
            reverse=True,
        )[:STYLE_TOP_N],
    }


# =====================================================================
# 🚀 FastAPI 接口
# =====================================================================
app = FastAPI(title="房产金融WebGIS租购决策仿真系统 V5.3")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_err_handler(request: Request, exc: RequestValidationError):
    err_msg = "; ".join([f"{err['loc'][-1]}: {err['msg']}" for err in exc.errors()])
    return JSONResponse(
        status_code=422, content={"code": 422, "detail": f"参数校验失败: {err_msg}"}
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.exception("全局异常")
    # 🌟 强制带上跨域头，防止报错被浏览器当成跨域拦截
    return JSONResponse(
        status_code=500, 
        content={"code": 500, "detail": f"服务计算异常: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"}
    )


@app.post("/api/simulate")
def simulate_relative(req: SimulationRequest):
    if req.monthly_income_net <= req.monthly_expense:
        raise HTTPException(400, "税后月收入必须大于每月基础生活开销")

    start_time = time.time()
    engine = HousingFinanceEngine(req)
    result_list = []
    sk_down, sk_loan, total = 0, 0, 0

    LOAN_SCHEDULE_CACHE.clear()

    for ry in RENT_CANDIDATES:
        for ly in LOAN_CANDIDATES:
            if ly > SIM_TOTAL_YEARS - ry:
                sk_loan += 1
                continue

            # 对每个租贷年限组合，遍历 3 种首付策略
            for ds in DOWN_STRATEGIES:
                total += 1
                res = engine.single_scenario_sim(ry, ly, ds)
                if res is None:
                    sk_down += 1
                else:
                    result_list.append(res)

    if not result_list:
        raise HTTPException(
            400,
            f"无可行方案：共尝试 {total} 组；{sk_down} 组因首付不足或支出占比超 70% 触发风控红线被剔除，{sk_loan} 组贷款年限超限。建议提高首付或增加收入。",
        )

    ew = entropy_weight_multi(result_list)
    out_list = attach_comprehensive(result_list, ew)

    warning = None
    if len(out_list) < 10:
        warning = (
            "当前参数下可行方案极少（部分被70%支出红线强杀），说明该方案风险极高！"
        )

    top_asset = sorted(out_list, key=lambda x: x["liquid_asset_end_wan"], reverse=True)[
        :TOP_N
    ]
    top_adapt = sorted(out_list, key=lambda x: x["adapt_score"], reverse=True)[:TOP_N]
    top_comfort = sorted(out_list, key=lambda x: x["comfort_score"], reverse=True)[
        :TOP_N
    ]

    cost = time.time() - start_time
    logging.info(f"相对仿真完成，可行方案：{len(result_list)}组，耗时：{cost:.2f}s")

    return {
        "code": 200,
        "mode": "relative",
        "warning": warning,
        "assumptions": MODEL_ASSUMPTIONS,
        "entropy_result": ew,
        "all_scheme": out_list,
        "top_comprehensive": out_list[:TOP_N],
        "top_asset": top_asset,
        "top_adapt": top_adapt,
        "top_comfort": top_comfort,
        "diversified_by_rent": build_diversified_by_rent(out_list),
        "style_boards": build_style_boards(out_list),
        "skipped": {
            "by_downpay": sk_down,
            "by_loan_overrun": sk_loan,
            "total_attempted": total,
        },
    }


@app.post("/api/simulate/absolute")
def simulate_absolute(req: AbsoluteSimulationRequest):
    if req.monthly_income_net <= req.monthly_expense:
        raise HTTPException(400, "税后月收入必须大于每月基础生活开销")

    start_time = time.time()
    base_payload = req.model_dump()
    all_candidates = []
    feasible_by_level = {}
    seen_zero_rent_combos = set()

    LOAN_SCHEDULE_CACHE.clear()
    engine = HousingFinanceEngine(SimulationRequest(**base_payload))

    for level in RENT_LEVELS_FOR_ABSOLUTE:
        engine.set_base_rent(level)
        cnt = 0
        for ry in RENT_CANDIDATES:
            for ly in LOAN_CANDIDATES:
                if ly > SIM_TOTAL_YEARS - ry:
                    continue

                for ds in DOWN_STRATEGIES:
                    if ry == 0:
                        combo_key = (ry, ly, ds)
                        if combo_key in seen_zero_rent_combos:
                            continue
                        seen_zero_rent_combos.add(combo_key)

                    res = engine.single_scenario_sim(ry, ly, ds)
                    if res is not None:
                        res["rent_level"] = 0 if ry == 0 else level
                        all_candidates.append(res)
                        cnt += 1
        feasible_by_level[str(level)] = cnt

    if not all_candidates:
        raise HTTPException(
            400,
            "所有候选租金档位下均无可行方案。可能是您的收入无法覆盖 70% 支出的硬性红线。",
        )

    ew = entropy_weight_multi(all_candidates)
    out_list = attach_comprehensive(all_candidates, ew)

    warning = None
    if len(out_list) < 10:
        warning = "极端严苛的 70% 财务红线过滤了绝大多数平庸方案，当前留下的均为极限承压方案。"

    rent_board = {}
    for item in out_list:
        k = str(item["rent_level"])
        rent_board.setdefault(k, [])
        if len(rent_board[k]) < PER_GROUP_N:
            rent_board[k].append(item)

    cost = time.time() - start_time
    logging.info(
        f"绝对寻优完成，档位：{len(RENT_LEVELS_FOR_ABSOLUTE)}，可行方案：{len(all_candidates)}，耗时：{cost:.2f}s"
    )

    return {
        "code": 200,
        "mode": "absolute",
        "warning": warning,
        "assumptions": MODEL_ASSUMPTIONS,
        "entropy_result": ew,
        "global_best": out_list[0],
        "rent_level_board": rent_board,
        "all_scheme": out_list,
        "top_comprehensive": out_list[:TOP_N],
        "top_asset": sorted(
            out_list, key=lambda x: x["liquid_asset_end_wan"], reverse=True
        )[:TOP_N],
        "top_adapt": sorted(out_list, key=lambda x: x["adapt_score"], reverse=True)[
            :TOP_N
        ],
        "top_comfort": sorted(out_list, key=lambda x: x["comfort_score"], reverse=True)[
            :TOP_N
        ],
        "style_boards": build_style_boards(out_list),
        "cross_level_note": "绝对寻优模式：已启用多维首付策略自动组合。",
        "skipped_summary": {"feasible_by_rent_level": feasible_by_level},
    }


@app.get("/api/gis/regions")
def get_regions():
    return {"code": 200, "data": NANNING_REGIONS}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
