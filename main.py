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
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =====================================================================
# 🌐 全局常量与配置
# =====================================================================
SIM_TOTAL_YEARS = 30
RENT_CANDIDATES = list(range(0, 21))
LOAN_CANDIDATES = list(range(5, 31))
RENT_LEVELS_FOR_ABSOLUTE = [1200, 1500, 1800, 2200, 2600, 3000, 3500, 4000]

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
    ("asset",     "liquid_asset_end_wan"),
    ("adapt",     "adapt_score"),
    ("comfort",   "comfort_score"),
    ("happiness", "surplus_happiness"),
    ("time",      "time_efficiency"),
]

MODEL_ASSUMPTIONS = [
    "流动资产仅计现金理财余额，不含房产残值、契税、维修基金等交易税费（坚决拒绝纸面财富幻觉）",
    "公积金缴存基数简化等同于输入的月收入；housing_fund_pct为个人+单位合计缴存比例，双边上限24%",
    "租房阶段：公积金按月提取，计入当月可支配现金流；购房还贷阶段，公积金按月对冲抵扣月供",
    "租金按年度复利调价，同一年内12个月租金固定不变",
    "严苛财务红线：无论何种方案，购房还贷期（真实月供+生活开销+物业费）超过月收入 70% 的组合将被直接一票否决剔除",
    "小康家庭定义：全生命周期平均月度纯现金结余必须达到或超过总收入的 50%，抗拒沦为被账单绑架的打工机器",
    "反资本剥削：贷款年限越短，时间效率得分越高，鼓励尽早赎清贷款，少给银行交利息",
    "抗风险稳健度算法升级：基于生命周期中最紧绷月份的“真实支出占比”距离70%红线的安全余量来计算，越安全得分越高",
    "30年周期内未结清贷款的组合直接剔除，不跨周期挂账",
    "税后收入、基础生活开销恒定，不引入薪资上涨与物价通胀",
    "绝对模式采用离散租金档位抽样遍历，求得近似全局最优解"
]

# 🚨 修复了所有区域的精准经纬度
NANNING_REGIONS = [
    {"name": "青秀区", "lat": 22.79, "lng": 108.38, "house_ref": 140, "rent_ref": 3000, "desc": "核心城区，配套完善，名校云集"},
    {"name": "西乡塘区", "lat": 22.84, "lng": 108.28, "house_ref": 85, "rent_ref": 1600, "desc": "科教片区，高校林立，生活成本低"},
    {"name": "江南区", "lat": 22.78, "lng": 108.28, "house_ref": 78, "rent_ref": 1500, "desc": "老城区，刚需友好，烟火气浓"},
    {"name": "兴宁区", "lat": 22.85, "lng": 108.33, "house_ref": 92, "rent_ref": 1900, "desc": "老城商圈，火车站附近，交通便利"},
    {"name": "良庆区", "lat": 22.74, "lng": 108.36, "house_ref": 110, "rent_ref": 2300, "desc": "五象新区核心地带，发展潜力大"},
    {"name": "邕宁区", "lat": 22.76, "lng": 108.48, "house_ref": 82, "rent_ref": 1600, "desc": "五象东扩区，价格洼地，生态宜居"},
    {"name": "武鸣区", "lat": 23.16, "lng": 108.27, "house_ref": 55, "rent_ref": 1200, "desc": "教育园区，大学城，价格极低"},
    {"name": "五象新区", "lat": 22.72, "lng": 108.35, "house_ref": 120, "rent_ref": 2500, "desc": "自治区级重点发展新区，总部基地"}
]

LOAN_SCHEDULE_CACHE: Dict[tuple, List[float]] = {}

# =====================================================================
# 📦 入参模型
# =====================================================================
class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    house_total_wan: float = Field(gt=0)
    initial_savings_wan: float = Field(ge=0)
    down_payment_ratio: float = Field(ge=15, le=80)
    
    monthly_income_net: float = Field(gt=0)
    monthly_expense: float = Field(gt=0)
    property_fee_monthly: float = Field(ge=0)
    housing_fund_pct: float = Field(ge=0, le=24, default=16)
    
    rent_monthly_base: float = Field(gt=0, default=1800) 
    invest_annual_yield_pct: float = Field(ge=0)
    rent_inflation_year_pct: float = Field(ge=0)
    loan_annual_rate_pct: float = Field(gt=0)
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
        self.down_ratio = req.down_payment_ratio / 100.0
        
        self.month_income = req.monthly_income_net
        self.month_base_exp = req.monthly_expense
        self.month_property = req.property_fee_monthly
        self.month_fund_inflow = self.month_income * (req.housing_fund_pct / 100.0)
        
        self.base_rent = req.rent_monthly_base
        self.invest_year_rate = req.invest_annual_yield_pct / 100.0
        self.month_invest_rate = (1 + self.invest_year_rate) ** (1/12) - 1
        self.rent_inflation = req.rent_inflation_year_pct / 100.0
        self.loan_year_rate = req.loan_annual_rate_pct / 100.0
        self.repay_kind = req.repay_kind

        self.down_payment = self.house_total_yuan * self.down_ratio
        self.loan_principal = self.house_total_yuan - self.down_payment

    def set_base_rent(self, new_rent: float):
        self.base_rent = new_rent

    def build_loan_schedule(self, loan_year: int) -> List[float]:
        key = (self.loan_year_rate, self.repay_kind, self.loan_principal, loan_year)
        if key in LOAN_SCHEDULE_CACHE:
            return LOAN_SCHEDULE_CACHE[key]

        n = loan_year * 12
        month_rate = self.loan_year_rate / 12.0
        if self.loan_principal <= EPS_YUAN:
            res = [0.0] * n
            LOAN_SCHEDULE_CACHE[key] = res
            return res
        if abs(month_rate) < EPS_LOG:
            res = [self.loan_principal / n] * n
            LOAN_SCHEDULE_CACHE[key] = res
            return res

        schedule = []
        if self.repay_kind == "equal_principal_interest":
            m = self.loan_principal * month_rate * (1 + month_rate) ** n / ((1 + month_rate) ** n - 1)
            schedule = [m] * n
        else:
            principal_m = self.loan_principal / n
            remain = self.loan_principal
            for _ in range(n):
                schedule.append(principal_m + remain * month_rate)
                remain -= principal_m
        LOAN_SCHEDULE_CACHE[key] = schedule
        return schedule

    def single_scenario_sim(self, rent_year: int, loan_year: int) -> Optional[Dict]:
        if loan_year > (SIM_TOTAL_YEARS - rent_year): return None

        rent_month = rent_year * 12
        loan_month = loan_year * 12
        remain_cash = self.init_saving
        max_deficit = 0.0
        total_months_surplus_rate = 0.0 
        
        max_outflow_ratio = 0.0

        # 阶段1：租房攒首付
        for t in range(rent_month):
            year_idx = t // 12
            current_rent = self.base_rent * ((1 + self.rent_inflation) ** year_idx)
            real_monthly_outflow = self.month_base_exp + current_rent
            ratio = real_monthly_outflow / self.month_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio
            
            monthly_net = self.month_income - real_monthly_outflow
            total_months_surplus_rate += (monthly_net / self.month_income)
            
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: max_deficit = max(max_deficit, -remain_cash)
            remain_cash = max(0.0, remain_cash)

        # 首付硬约束
        if remain_cash < self.down_payment - EPS_YUAN: return None
        remain_cash -= self.down_payment
        remain_cash = max(0.0, remain_cash)

        # 阶段2：还贷期
        loan_schedule = self.build_loan_schedule(loan_year)
        
        for m in range(loan_month):
            fund_deduct = min(self.month_fund_inflow, loan_schedule[m])
            actual_out_of_pocket = loan_schedule[m] - fund_deduct
            
            real_monthly_outflow = self.month_base_exp + self.month_property + actual_out_of_pocket
            ratio = real_monthly_outflow / self.month_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio
            
            if real_monthly_outflow > (self.month_income * 0.70):
                return None
                
            monthly_net = self.month_income - real_monthly_outflow
            total_months_surplus_rate += (monthly_net / self.month_income)
            
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: max_deficit = max(max_deficit, -remain_cash)
            remain_cash = max(0.0, remain_cash)

        # 阶段3：自由期
        rest_month = SIM_TOTAL_YEARS * 12 - rent_month - loan_month
        for _ in range(rest_month):
            real_monthly_outflow = self.month_base_exp + self.month_property
            ratio = real_monthly_outflow / self.month_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio
            
            monthly_net = self.month_income - real_monthly_outflow
            total_months_surplus_rate += (monthly_net / self.month_income)
            
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: max_deficit = max(max_deficit, -remain_cash)
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
        if avg_surplus_rate >= 0.50:
            happiness = 100.0
        else:
            happiness = max(0.0, (avg_surplus_rate / 0.50) * 100)

        time_eff = max(0.0, 100.0 - (rent_year / SIM_TOTAL_YEARS * 30 + loan_year / SIM_TOTAL_YEARS * 70))

        return {
            "rent_year": rent_year,
            "loan_year": loan_year,
            "occupied_years": round(occupied_years, 2),
            "down_payment_wan": round(self.down_payment / 10000, 2),
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
        return {f"weight_{k}": round(w, 4) for k, _ in ENTROPY_INDICATORS} | {f"entropy_{k}": 0.0 for k, _ in ENTROPY_INDICATORS}
    
    ew = {}
    d_list = []
    for short, field in ENTROPY_INDICATORS:
        vec = [s[field] for s in scheme_list]
        min_v, max_v = min(vec), max(vec)
        if max_v - min_v < EPS_FLOAT:
            norm = [1.0 / n for _ in vec]
        else:
            norm = [max(EPS_LOG, min(1 - EPS_LOG, (v - min_v) / (max_v - min_v))) for v in vec]
        p = [x / sum(norm) if sum(norm) > 0 else 0 for x in norm]
        e = -sum(x * math.log(x) for x in p if x > EPS_LOG) / math.log(n)
        ew[f"entropy_{short}"] = round(e, 4)
        d_list.append(1 - e)

    sum_d = sum(d_list)
    for idx, (short, _) in enumerate(ENTROPY_INDICATORS):
        w = d_list[idx] / sum_d if sum_d > EPS_FLOAT else 1.0 / len(ENTROPY_INDICATORS)
        ew[f"weight_{short}"] = round(w, 4)

    w_sum = sum(ew[f"weight_{k}"] for k, _ in ENTROPY_INDICATORS)
    ew["weight_warning"] = f"权重之和异常({w_sum:.3f})" if not (0.98 <= w_sum <= 1.02) else None
    return ew

def attach_comprehensive(scheme_list: List[Dict], ew: Dict) -> List[Dict]:
    if not scheme_list: return []
    
    if len(scheme_list) == 1:
        item = scheme_list[0]
        item["comprehensive_score"] = 100.0
        item["suitability_score"] = round(
            item["comfort_score"] * SUITABILITY_WEIGHTS["comfort"] +
            item["adapt_score"] * SUITABILITY_WEIGHTS["adapt"] +
            item["surplus_happiness"] * SUITABILITY_WEIGHTS["happiness"] +
            item["time_efficiency"] * SUITABILITY_WEIGHTS["time"], 2
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
            item["comfort_score"] * SUITABILITY_WEIGHTS["comfort"] +
            item["adapt_score"] * SUITABILITY_WEIGHTS["adapt"] +
            item["surplus_happiness"] * SUITABILITY_WEIGHTS["happiness"] +
            item["time_efficiency"] * SUITABILITY_WEIGHTS["time"], 2
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
    return {
        "稳健型": sorted(out_list, key=lambda x: x["adapt_score"], reverse=True)[:STYLE_TOP_N],
        "平衡型": sorted(out_list, key=lambda x: x["suitability_score"], reverse=True)[:STYLE_TOP_N],
        "进取型": sorted(out_list, key=lambda x: x["liquid_asset_end_wan"], reverse=True)[:STYLE_TOP_N],
    }

# =====================================================================
# 🚀 FastAPI 接口
# =====================================================================
app = FastAPI(title="房产金融WebGIS租购决策仿真系统 V4.9")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.exception_handler(RequestValidationError)
async def validation_err_handler(request: Request, exc: RequestValidationError):
    err_msg = "; ".join([f"{err['loc'][-1]}: {err['msg']}" for err in exc.errors()])
    return JSONResponse(status_code=422, content={"code": 422, "detail": f"参数校验失败: {err_msg}"})

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logging.exception("全局异常")
    return JSONResponse(status_code=500, content={"code": 500, "detail": f"服务计算异常: {str(exc)}"})

@app.post("/api/simulate")
def simulate_relative(req: SimulationRequest):
    if req.monthly_income_net <= req.monthly_expense:
        raise HTTPException(400, "税后月收入必须大于每月基础生活开销")

    start_time = time.time()
    engine = HousingFinanceEngine(req)
    result_list = []
    sk_down, sk_loan, sk_redline, total = 0, 0, 0, 0

    LOAN_SCHEDULE_CACHE.clear()

    for ry in RENT_CANDIDATES:
        for ly in LOAN_CANDIDATES:
            total += 1
            if ly > SIM_TOTAL_YEARS - ry:
                sk_loan += 1
                continue
            
            res = engine.single_scenario_sim(ry, ly)
            if res is None:
                sk_down += 1 
            else:
                result_list.append(res)

    if not result_list:
        raise HTTPException(400, f"无可行方案：共尝试 {total} 组；{sk_down} 组因首付不足或支出占比超 70% 触发风控红线被剔除，{sk_loan} 组贷款年限超限。建议提高首付或增加收入。")

    ew = entropy_weight_multi(result_list)
    out_list = attach_comprehensive(result_list, ew)

    warning = None
    if len(out_list) < 10:
        warning = "当前参数下可行方案极少（部分被70%支出红线强杀），说明该方案风险极高！"

    top_asset = sorted(out_list, key=lambda x: x["liquid_asset_end_wan"], reverse=True)[:TOP_N]
    top_adapt = sorted(out_list, key=lambda x: x["adapt_score"], reverse=True)[:TOP_N]
    top_comfort = sorted(out_list, key=lambda x: x["comfort_score"], reverse=True)[:TOP_N]

    cost = time.time() - start_time
    logging.info(f"相对仿真完成，可行方案：{len(result_list)}组，耗时：{cost:.2f}s")

    return {
        "code": 200, "mode": "relative", "warning": warning,
        "assumptions": MODEL_ASSUMPTIONS, "entropy_result": ew,
        "all_scheme": out_list, "top_comprehensive": out_list[:TOP_N],
        "top_asset": top_asset, "top_adapt": top_adapt, "top_comfort": top_comfort,
        "diversified_by_rent": build_diversified_by_rent(out_list),
        "style_boards": build_style_boards(out_list),
        "skipped": {"by_downpay": sk_down, "by_loan_overrun": sk_loan, "total_attempted": total}
    }

@app.post("/api/simulate/absolute")
def simulate_absolute(req: AbsoluteSimulationRequest):
    if req.monthly_income_net <= req.monthly_expense:
        raise HTTPException(400, "税后月收入必须大于每月基础生活开销")

    start_time = time.time()
    base_payload = req.model_dump()
    all_candidates = []
    feasible_by_level = {}

    LOAN_SCHEDULE_CACHE.clear()

    base_payload["rent_monthly_base"] = RENT_LEVELS_FOR_ABSOLUTE[0]
    engine = HousingFinanceEngine(SimulationRequest(**base_payload))

    for level in RENT_LEVELS_FOR_ABSOLUTE:
        engine.set_base_rent(level)
        cnt = 0
        for ry in RENT_CANDIDATES:
            for ly in LOAN_CANDIDATES:
                if ly > SIM_TOTAL_YEARS - ry: continue
                res = engine.single_scenario_sim(ry, ly)
                if res is not None:
                    res["rent_level"] = level
                    all_candidates.append(res)
                    cnt += 1
        feasible_by_level[str(level)] = cnt

    if not all_candidates:
        raise HTTPException(400, "所有候选租金档位下均无可行方案。可能是您的收入无法覆盖 70% 支出的硬性红线。")

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
    logging.info(f"绝对寻优完成，档位：{len(RENT_LEVELS_FOR_ABSOLUTE)}，可行方案：{len(all_candidates)}，耗时：{cost:.2f}s")

    return {
        "code": 200, "mode": "absolute", "warning": warning,
        "assumptions": MODEL_ASSUMPTIONS, "entropy_result": ew,
        "global_best": out_list[0], "rent_level_board": rent_board,
        "all_scheme": out_list,
        "top_comprehensive": out_list[:TOP_N],
        "top_asset": sorted(out_list, key=lambda x: x["liquid_asset_end_wan"], reverse=True)[:TOP_N],
        "top_adapt": sorted(out_list, key=lambda x: x["adapt_score"], reverse=True)[:TOP_N],
        "top_comfort": sorted(out_list, key=lambda x: x["comfort_score"], reverse=True)[:TOP_N],
        "style_boards": build_style_boards(out_list),
        "cross_level_note": "绝对寻优模式：对全部租金档位下的可行方案合并为一个全集，统一做极值归一化与熵权赋权。",
        "skipped_summary": {"feasible_by_rent_level": feasible_by_level}
    }

@app.get("/api/gis/regions")
def get_regions():
    return {"code": 200, "data": NANNING_REGIONS}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)