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

try:
    import db as data_layer
except Exception:  # noqa: BLE001
    data_layer = None

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

# =====================================================================
# 🏙️ 空间异质性系数（V2.0 核心）
# ---------------------------------------------------------------------
# 队长最核心的洞察：“同样的钱，在南宁和在北上广，买到的是两种人生。”
# 不把城市能级折算进模型，全国 WebGIS 就只是一张好看的壁纸。
# =====================================================================
CITY_TIER_LABELS = {
    1: "超一线",
    2: "新一线",
    3: "二线",
    4: "三线",
    5: "四线及以下",
}

# 城市能级 → 资源溢价贴现（医疗/教育/就业天花板的相对价值，1.0 为天花板）
CITY_RESOURCE_INDEX = {1: 1.00, 2: 0.94, 3: 0.88, 4: 0.80, 5: 0.72}

# 城市能级 → 生活成本指数（同样 2200 元，在超一线只能算“活着”）
CITY_COST_INDEX = {1: 1.28, 2: 1.14, 3: 1.00, 4: 0.90, 5: 0.82}

# 通勤时间成本折现：单程分钟数 → 0~100 分（15 分钟满分区，60 分钟后加速惩罚）
COMMUTE_FREE_MIN = 15
COMMUTE_PENALTY_PER_MIN = 1.25
COMMUTE_HEAVY_MIN = 60
COMMUTE_HEAVY_EXTRA = 0.8

# 空间舒适度、通勤得分在最终五维中的占比
COMFORT_SPATIAL_WEIGHT = 0.55
TIME_COMMUTE_WEIGHT = 0.55

FAMILY_LABELS = {1: "单身贵族", 2: "二人世界", 3: "三口之家"}

# =====================================================================
# 📈 职业薪资曲线（V6.3）
# ---------------------------------------------------------------------
# 队长指出：原“前5年复利、6-15年减半、15年后0.5%”仍然是指数骨架，
# 3% 复利 30 年 = 2.43 倍，对普通企业员工明显偏高。
#
# 现实是一条会撞天花板的 S 曲线：前几年爬坡、中期晋升、后期平台。
# 锚点来自国家统计局《2024年城镇单位就业人员年平均工资情况》：
#   · 城镇非私营单位 124,110 元/年（名义 +2.8%）
#   · 教育行业 126,185 元/年（+1.7%）
#   · 信息传输/软件 238,966 元/年（+3.1%）
# 以及一个常识：起薪越低，相对成长空间越大（4000→8000 的翻倍，
# 远比 20000→40000 常见）。
# =====================================================================
CAREER_TYPES = {
    "public": {
        "label": "体制内 / 事业单位",
        "cap_bonus": -0.05,
        "mid_bonus": 2.0,
        "note": "职称台阶：前期慢、后期稳，天花板不高但很少掉下来",
    },
    "corporate": {
        "label": "企业职员",
        "cap_bonus": 0.00,
        "mid_bonus": 0.0,
        "note": "早快后平：35 岁前后到平台，天花板由行业决定",
    },
    "freelance": {
        "label": "自由职业 / 销售",
        "cap_bonus": 0.25,
        "mid_bonus": -2.0,
        "note": "上限更高，但收入波动大，需要更厚的现金缓冲",
    },
}

# 行业细分：用国家统计局 2024 年分行业年平均工资相对全国均值（124,110 元）校准
INDUSTRY_PROFILES = {
    "it":            {"label": "信息技术 / 互联网", "wage_ratio": 1.93, "cap_factor": 1.15},
    "finance":       {"label": "金融",             "wage_ratio": 1.63, "cap_factor": 1.10},
    "research":      {"label": "科研 / 技术服务",   "wage_ratio": 1.41, "cap_factor": 1.08},
    "health":        {"label": "医疗 / 卫生",       "wage_ratio": 1.15, "cap_factor": 1.03},
    "education":     {"label": "教育",             "wage_ratio": 1.02, "cap_factor": 1.00},
    "public_admin":  {"label": "公共管理",          "wage_ratio": 0.93, "cap_factor": 0.98},
    "manufacturing": {"label": "制造 / 建筑",       "wage_ratio": 0.87, "cap_factor": 0.95},
    "services":      {"label": "住餐 / 居民服务",   "wage_ratio": 0.49, "cap_factor": 0.85},
    "other":         {"label": "其他 / 综合",       "wage_ratio": 1.00, "cap_factor": 1.00},
}

# 职业类型的收入波动率（用于计算应急现金缓冲月数）
CAREER_VOLATILITY = {"public": 0.05, "corporate": 0.10, "freelance": 0.30}

CAREER_CAP_BASE = 2.10        # 起薪 4000 时的上限倍数
CAREER_CAP_SLOPE = 0.55       # 起薪每翻一倍，上限倍数下降的斜率
CAREER_MIN_MULTIPLE = 1.35
CAREER_MAX_MULTIPLE = 2.40
CAREER_TIER_BONUS = {1: 0.12, 2: 0.06, 3: 0.00, 4: -0.05, 5: -0.10}
CAREER_S_SCALE = 4.5          # S 曲线陡峭度


def career_cap_multiple(start_income: float, city_tier: int = 3, career_type: str = "corporate", industry: str = "other") -> float:
    """起薪 → 职业天花板倍数：起薪越低，相对成长空间越大；职业类型微调。"""
    start = max(1000.0, float(start_income))
    m = CAREER_CAP_BASE - CAREER_CAP_SLOPE * math.log(start / 4000.0)
    m += CAREER_TIER_BONUS.get(int(city_tier), 0.0)
    m += CAREER_TYPES.get(career_type, CAREER_TYPES["corporate"])["cap_bonus"]
    m *= INDUSTRY_PROFILES.get(industry, INDUSTRY_PROFILES["other"])["cap_factor"]
    return max(CAREER_MIN_MULTIPLE, min(CAREER_MAX_MULTIPLE, m))


# =====================================================================
# 🧭 风险姿态（V6.1）
# ---------------------------------------------------------------------
# 队长定调：70% 红线是默认承诺，不能被“优化”掉；
# 90% 只属于一条显式的、需要用户自己按下的分支——悬崖方案。
# =====================================================================
RISK_MODES = {
    "steady": {
        "label": "稳健红线",
        "loan_cap": 0.70,
        "rent_cap": None,   # 租房期只受现金流约束，压力通过抗风险得分体现
        "note": "70% 还贷红线 + 隐形支出摊销，为生病、失业、人情往来留出 30% 的呼吸空间",
    },
    "aggressive": {
        "label": "激进·悬崖方案",
        "loan_cap": 0.90,
        "rent_cap": 0.90,   # 擦着生存线过，不给自己留退路
        "note": "90% 生存线：用安全垫换时间，任何一次意外都可能让你踩空",
    },
}

# 激进模式下可选的“节衣缩食”：把可支配生活开销压缩到这个比例
SQUEEZE_EXPENSE_FACTOR = 0.80

# 隐形支出：不作为默认值强加给用户，默认 0。
# 我们只负责提醒“请自己估算红包/旅行/医疗/意外”，由用户决定填多少。
DEFAULT_HIDDEN_EXPENSE_MONTH = 0.0

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
    "小康家庭定义：全周期平均月度纯现金结余必须达到或超过总收入的 50%，抗拒沦为被账单绑架的打工机器",
    "反资本剥削：贷款年限越短、首付比例越高，时间效率得分越高，鼓励尽早赎清贷款",
    "抗风险稳健度算法升级：基于周期中最紧绷月份的“真实支出占比”距离70%红线的安全余量来计算",
    "首付策略升级：输入的首付比例仅作为最低门槛，系统会智能遍历最低首付、折中首付、极限高首付（掏空现金）策略",
    "30年周期内未结清贷款的组合直接剔除，不跨周期挂账",
    "绝对模式采用离散租金档位抽样遍历，保留全部0至20年租期",
    "空间异质性折算：房价按片区真实单价（元/㎡）折算可购面积，居住舒适度50%以上由“人均居住面积×城市资源溢价”决定，拒绝用总价幻想豪宅",
    "通勤折现：租房期与还贷期分别使用各自的单程通勤分钟，按周期加权得到时间效率；单程15分钟为满分，60分钟后进入加速惩罚区",
    "通勤交通费按月计入现金流：租房期与买房期分别扣减，默认按 120元 + 4元/分钟 估算，可显式覆盖",
    "相对收入位置：结余幸福感乘以本地收入分位系数（你的收入/城市平均薪资），同样的结余在不同城市体感完全不同",
    "稳健模式：70%红线只用于还贷期；租房期不设硬顶，租房压力通过抗风险得分体现",
    "激进模式（悬崖方案）：租贷两期都放宽到90%生存线，追求最快时间效率，代价是几乎没有安全垫",
    "隐形支出不设默认值：红包、旅行、医疗、意外、人情往来由用户自行估算（建议覆盖年度人情与意外），填 0 表示暂不计入",
    "薪资为 S 型曲线而非指数复利：起薪决定职业天花板（4000 起薪约 2.2 倍、10000 起薪约 1.6 倍），增长输入是实际增速，名义工资再叠加通胀",
    "薪资曲线锚点参考国家统计局 2024 年数据：城镇非私营单位年均 124,110 元、教育业 126,185 元、信息业 238,966 元，名义增速 2.8%、1.7%、3.1%",
    "30年期末流动资产额外给出“按今日物价折算的真实购买力”，戳破名义资产的纸面幻觉",
    "房价按当前名义价格静止处理，不做增值假设：我们只推演现金流与真实居住体感，不制造“房价永远涨”的纸面财富",
]

NANNING_REGIONS = [
    {
        "name": "青秀区",
        "lat": 22.79,
        "lng": 108.38,
        "house_ref": 140,
        "rent_ref": 3000,
        "price_sqm": 15500,
        "commute_min": 15,
        "desc": "核心城区，配套完善，名校云集",
    },
    {
        "name": "西乡塘区",
        "lat": 22.84,
        "lng": 108.28,
        "house_ref": 85,
        "rent_ref": 1600,
        "price_sqm": 9500,
        "commute_min": 28,
        "desc": "科教片区，高校林立，生活成本低",
    },
    {
        "name": "江南区",
        "lat": 22.78,
        "lng": 108.28,
        "house_ref": 78,
        "rent_ref": 1500,
        "price_sqm": 8700,
        "commute_min": 30,
        "desc": "老城区，刚需友好，烟火气浓",
    },
    {
        "name": "兴宁区",
        "lat": 22.85,
        "lng": 108.33,
        "house_ref": 92,
        "rent_ref": 1900,
        "price_sqm": 10200,
        "commute_min": 22,
        "desc": "老城商圈，火车站附近，交通便利",
    },
    {
        "name": "良庆区",
        "lat": 22.74,
        "lng": 108.36,
        "house_ref": 110,
        "rent_ref": 2300,
        "price_sqm": 12200,
        "commute_min": 25,
        "desc": "五象新区核心地带，发展潜力大",
    },
    {
        "name": "邕宁区",
        "lat": 22.76,
        "lng": 108.48,
        "house_ref": 82,
        "rent_ref": 1600,
        "price_sqm": 9100,
        "commute_min": 35,
        "desc": "五象东扩区，价格洼地，生态宜居",
    },
    {
        "name": "武鸣区",
        "lat": 23.16,
        "lng": 108.27,
        "house_ref": 55,
        "rent_ref": 1200,
        "price_sqm": 6100,
        "commute_min": 55,
        "desc": "教育园区，大学城，价格极低",
    },
    {
        "name": "五象新区",
        "lat": 22.72,
        "lng": 108.35,
        "house_ref": 120,
        "rent_ref": 2500,
        "price_sqm": 13300,
        "commute_min": 20,
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
    # 🧱 V7.4 装修：买毛坯房的人，首付之外还有一笔硬装钱
    house_decoration: str = Field(default="finished", description="rough 毛坯 / finished 已装修（默认不装修）")
    reno_level: str = Field(default="standard", description="none 毛坯入住 / simple / standard / quality")
    reno_budget_wan: float = Field(ge=0, default=0, description="装修预算（万元）；0 = 按当地水平自动估算")

    # 🧭 V6.1 风险姿态与隐形支出
    risk_mode: str = Field(pattern="^(steady|aggressive)$", default="steady")
    squeeze_life: bool = Field(default=False)   # 仅激进模式生效：节衣缩食
    hidden_expense_month: float = Field(
        ge=0, default=DEFAULT_HIDDEN_EXPENSE_MONTH
    )  # 红包/旅行/医疗/意外 的月度摊销
    # 📈 职业类型与行业：决定薪资曲线的形状与天花板
    career_type: str = Field(pattern="^(public|corporate|freelance)$", default="corporate")
    industry: str = Field(default="other")
    income_volatility_pct: float = Field(ge=0, le=100, default=0)  # 0=按职业类型自动
    income_earners: int = Field(ge=1, le=4, default=1)  # 家庭就业人数（双人模式=2）
    house_price_growth_pct: float = Field(ge=0, le=20, default=0)  # 0=不预测涨跌

    # 🏙️ V2.0 空间异质性参数（前端由城市库与几何通勤计算注入）
    city_code: str = Field(default="450100")
    city_name: str = Field(default="南宁")
    city_tier: int = Field(ge=1, le=5, default=3)
    city_avg_price_sqm: float = Field(gt=0, default=10500)
    city_avg_rent_sqm: float = Field(gt=0, default=27)
    city_avg_salary: float = Field(gt=0, default=7200)
    district_price_sqm: float = Field(gt=0, default=10500)
    family_size: int = Field(ge=1, le=6, default=2)
    rent_commute_min: float = Field(ge=0, le=180, default=20)
    own_commute_min: float = Field(ge=0, le=180, default=40)
    rent_transport_month: float = Field(ge=0, default=0)
    own_transport_month: float = Field(ge=0, default=0)


    # 金融假设
    housing_fund_pct: float = Field(ge=0, le=24, default=16)
    provident_loan_cap_wan: float = Field(ge=0, default=60.0)
    annual_inflation_pct: float = Field(ge=0, default=2.5)

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
        self.income_growth_rate = req.annual_income_growth_pct / 100.0  # 实际增速（已扣通胀）

        # 👫 收入口径：家庭合计收入 / 就业人数 = 单人起薪
        self.income_earners = max(1, int(req.income_earners))
        self.per_earner_income = self.base_income / self.income_earners

        # 📈 职业天花板：起薪越低，相对成长空间越大；职业类型与行业微调
        self.career_type = req.career_type if req.career_type in CAREER_TYPES else "corporate"
        self.career_conf = CAREER_TYPES[self.career_type]
        self.industry = req.industry if req.industry in INDUSTRY_PROFILES else "other"
        self.industry_conf = INDUSTRY_PROFILES[self.industry]
        self.career_multiple = career_cap_multiple(
            self.per_earner_income, req.city_tier, self.career_type, self.industry
        )
        self.career_ceiling = self.per_earner_income * self.career_multiple * self.income_earners

        # 💨 收入波动 → 应急现金缓冲要求（3~7 个月）
        self.volatility = (
            req.income_volatility_pct / 100.0
            if req.income_volatility_pct > 0
            else CAREER_VOLATILITY.get(self.career_type, 0.10)
        )
        self.buffer_months_required = round(3.0 + 12.0 * self.volatility, 1)
        self.house_growth = req.house_price_growth_pct / 100.0

        # =============================================================
        # 🧭 V6.1 风险姿态：红线不是参数，是承诺
        # =============================================================
        self.risk_mode = req.risk_mode if req.risk_mode in RISK_MODES else "steady"
        self.risk_conf = RISK_MODES[self.risk_mode]
        self.loan_cap = self.risk_conf["loan_cap"]
        self.rent_cap = self.risk_conf["rent_cap"]
        self.squeeze_life = bool(req.squeeze_life) and self.risk_mode == "aggressive"
        self.expense_factor = SQUEEZE_EXPENSE_FACTOR if self.squeeze_life else 1.0
        self.hidden_expense_month = req.hidden_expense_month

        self.month_base_exp = req.monthly_expense * self.expense_factor
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
        self.consumption_inflation = req.annual_inflation_pct / 100.0

        self.loan_year_rate = req.loan_annual_rate_pct / 100.0
        self.prov_loan_rate = req.provident_loan_rate_pct / 100.0
        self.prov_cap_yuan = req.provident_loan_cap_wan * 10000
        self.repay_kind = req.repay_kind
        self.purchase_tax_rate = req.purchase_tax_rate / 100.0

        # 🧱 V7.4 装修：毛坯房在首付之外还有一笔钱
        #    · rough = 毛坯，要装修；finished = 已装修（二手房默认），我们不替用户算
        #    · reno_budget_wan > 0 用用户填的；= 0 按当地水平自动估算
        #    · reno_level = none 表示毛坯入住 / 自己找人装，模型不扣钱
        self.house_decoration = req.house_decoration if req.house_decoration in ("rough", "finished") else "finished"
        self.reno_level = req.reno_level if req.reno_level in ("none", "simple", "standard", "quality") else "standard"
        self.reno_budget_yuan = max(0.0, float(req.reno_budget_wan)) * 10000
        self.reno_enabled = self.house_decoration == "rough" and self.reno_level != "none"
        self.reno_unit_sqm = 0.0

        # =============================================================
        # 🏙️ V2.0 空间异质性：把“地域”折算成可计算的系数
        # =============================================================
        self.city_name = req.city_name
        self.city_tier = req.city_tier
        self.city_avg_price_sqm = req.city_avg_price_sqm
        self.city_avg_rent_sqm = req.city_avg_rent_sqm
        self.city_avg_salary = req.city_avg_salary
        self.district_price_sqm = req.district_price_sqm
        self.family_size = max(1, req.family_size)

        # 总价 → 真实可购面积（㎡）→ 人均居住面积（㎡/人）
        # 这是“85 万在南宁是两居，在北京是远郊老破小”的数学表达
        self.own_area_sqm = self.house_total_yuan / max(self.district_price_sqm, EPS_YUAN)
        self.area_per_person = self.own_area_sqm / self.family_size

        # 居住体感分：10㎡/人 = 30 分，45㎡/人 = 100 分（线性，封顶）
        self.own_comfort_spatial = min(
            100.0, max(0.0, 30.0 + (self.area_per_person - 10.0) * 2.0)
        )

        # 当地“体面两居”的月租基准（70㎡），用于衡量租房阶段的相对体感
        self.local_2bed_rent = max(self.city_avg_rent_sqm * 70.0, EPS_YUAN)

        # 通勤：租房期与买房期分别计算，时间成本按周期加权
        self.rent_commute_min = req.rent_commute_min
        self.own_commute_min = req.own_commute_min
        self.rent_commute_score = self.commute_score(req.rent_commute_min)
        self.own_commute_score = self.commute_score(req.own_commute_min)
        self.rent_transport_month = (
            req.rent_transport_month
            if req.rent_transport_month > 0
            else 120.0 + 4.0 * req.rent_commute_min
        )
        self.own_transport_month = (
            req.own_transport_month
            if req.own_transport_month > 0
            else 120.0 + 4.0 * req.own_commute_min
        )

        # 相对收入位置：你在本地到底是“人上人”还是“被平均”
        self.rel_income = self.base_income / max(self.city_avg_salary * self.income_earners, EPS_YUAN)
        self.cost_index = CITY_COST_INDEX.get(self.city_tier, 1.0)
        self.resource_index = CITY_RESOURCE_INDEX.get(self.city_tier, 0.85)

        # 通勤时间货币化：按当地时薪折算（21.75 工作日 × 8 小时）
        self.hourly_wage = self.base_income / (21.75 * 8.0)

    @staticmethod
    def estimate_reno_unit(avg_salary: float, district_price_sqm: float,
                           city_tier: int, area_sqm: float, level: str = "standard") -> float:
        """装修单价估算（元/㎡，建筑面积口径，不含家具家电）。实现见 reno.py。"""
        try:
            import reno as _reno
        except ImportError:  # pragma: no cover
            return 0.0
        return _reno.estimate_unit(avg_salary, district_price_sqm, city_tier, area_sqm, level)

    def reno_total_yuan(self) -> float:
        """这套房子的装修总价：用户填了就用用户的，否则按当地水平估。"""
        if not self.reno_enabled:
            self.reno_unit_sqm = 0.0
            return 0.0
        area = self.house_total_yuan / max(self.district_price_sqm, EPS_YUAN)
        if self.reno_budget_yuan > 0:
            self.reno_unit_sqm = self.reno_budget_yuan / max(area, 1.0)
            return self.reno_budget_yuan
        self.reno_unit_sqm = self.estimate_reno_unit(
            self.city_avg_salary, self.district_price_sqm, self.city_tier, area, self.reno_level
        )
        return self.reno_unit_sqm * area

    @staticmethod
    def commute_score(minutes: float) -> float:
        """单程通勤分钟 → 0~100 分（15 分钟满分，60 分钟后加速惩罚）。"""
        m = max(0.0, float(minutes))
        penalty = max(0.0, m - COMMUTE_FREE_MIN) * COMMUTE_PENALTY_PER_MIN
        if m > COMMUTE_HEAVY_MIN:
            penalty += (m - COMMUTE_HEAVY_MIN) * COMMUTE_HEAVY_EXTRA
        return max(5.0, min(100.0, 100.0 - penalty))

    def risk_summary(self) -> Dict:
        return {
            "mode": self.risk_mode,
            "label": self.risk_conf["label"],
            "loan_cap_pct": int(round(self.loan_cap * 100)),
            "rent_cap_pct": int(round(self.rent_cap * 100)) if self.rent_cap is not None else None,
            "squeeze_life": self.squeeze_life,
            "expense_factor": self.expense_factor,
            "hidden_expense_month": self.hidden_expense_month,
            "note": self.risk_conf["note"],
        }

    def spatial_summary(self) -> Dict:
        """输出城市级空间折算结果（与具体租贷组合无关，供前端叙事使用）。"""
        infl_factor = (1 + self.consumption_inflation) ** SIM_TOTAL_YEARS
        pir = self.house_total_yuan / max(self.base_income * 12.0, EPS_YUAN)
        rent_yield = (
            self.base_rent * 12.0 / self.house_total_yuan * 100.0
            if self.house_total_yuan > 0
            else 0.0
        )
        commute_cost_month = (
            self.own_commute_min * 2 * 22 / 60.0 * self.hourly_wage
        )
        commute_cost_month_rent = (
            self.rent_commute_min * 2 * 22 / 60.0 * self.hourly_wage
        )
        return {
            "city_name": self.city_name,
            "city_tier": self.city_tier,
            "tier_label": CITY_TIER_LABELS.get(self.city_tier, "未知"),
            "resource_index": round(self.resource_index, 3),
            "cost_index": round(self.cost_index, 3),
            "district_price_sqm": round(self.district_price_sqm, 1),
            "city_avg_price_sqm": round(self.city_avg_price_sqm, 1),
            "city_avg_salary": round(self.city_avg_salary, 1),
            "rel_income": round(self.rel_income, 3),
            "family_size": self.family_size,
            "family_label": FAMILY_LABELS.get(self.family_size, "自定义"),
            "own_area_sqm": round(self.own_area_sqm, 1),
            "area_per_person": round(self.area_per_person, 1),
            "own_comfort_spatial": round(self.own_comfort_spatial, 2),
            "local_2bed_rent": round(self.local_2bed_rent, 0),
            "commute": {
                "rent_min": self.rent_commute_min,
                "own_min": self.own_commute_min,
                "rent_score": round(self.rent_commute_score, 2),
                "own_score": round(self.own_commute_score, 2),
                "time_cost_month": round(commute_cost_month, 0),
                "time_cost_month_rent": round(commute_cost_month_rent, 0),
                "transport_month": {
                    "rent": round(self.rent_transport_month, 0),
                    "own": round(self.own_transport_month, 0),
                },
            },
            "pir": round(pir, 2),
            "rent_yield_pct": round(rent_yield, 3),
            "inflation_factor_30y": round(infl_factor, 3),
            "area_note": (
                f"按 {self.district_price_sqm:,.0f} 元/㎡ 折算，"
                f"{self.house_total_yuan / 10000:.0f} 万可购约 {self.own_area_sqm:.1f} ㎡，"
                f"人均 {self.area_per_person:.1f} ㎡"
            ),
        }

    def set_base_rent(self, new_rent: float):
        self.base_rent = new_rent

    # 📈 职业 S 曲线：起薪决定天花板，增长输入决定爬坡速度
    def career_mid_year(self) -> float:
        """增长越快，越早到达晋升平台（但天花板不变）。"""
        return max(4.0, min(16.0, 14.0 - self.income_growth_rate * 100.0 * 1.5 + self.career_conf["mid_bonus"]))

    def _career_progress(self, year_idx: int) -> float:
        """0~1 的 S 曲线进度：前段慢、中段快、后段趋平。"""
        if self.income_growth_rate <= 0 or self.career_ceiling <= self.base_income + 1:
            return 0.0
        mid = self.career_mid_year()
        s = CAREER_S_SCALE

        def f(t):
            return 1.0 / (1.0 + math.exp(-(t - mid) / s))

        f0, f30 = f(0), f(30)
        if f30 - f0 < EPS_FLOAT:
            return 1.0
        shape = max(0.0, min(1.0, (f(year_idx) - f0) / (f30 - f0)))
        # 增速决定 30 年后能吃到多少天花板：3% 吃满，1% 只吃三分之一
        end_fraction = min(1.0, self.income_growth_rate / 0.03)
        return shape * end_fraction

    def salary_curve(self, year_idx: int) -> float:
        """
        模型口径的“名义工资”：S 曲线，撞天花板后不再上涨。
        刻意不做通胀指数化——30 年后名义 1.6 万，按今日购买力可能只剩七八千。
        这不是悲观，是拒绝用“工资永远跑赢通胀”的假设去画大饼。
        """
        return self.base_income + (
            self.career_ceiling - self.base_income
        ) * self._career_progress(year_idx)

    def get_current_income(self, year_idx: int) -> float:
        """现金流使用的名义工资。"""
        return self.salary_curve(year_idx)

    def career_summary(self) -> Dict:
        years = (0, 5, 10, 15, 20, 25, 30)
        infl = (1 + self.consumption_inflation) ** SIM_TOTAL_YEARS
        nominal_30 = self.salary_curve(SIM_TOTAL_YEARS)
        return {
            "start_monthly": round(self.base_income, 0),
            "income_earners": self.income_earners,
            "per_earner_start": round(self.per_earner_income, 0),
            "career_type": self.career_type,
            "career_label": self.career_conf["label"],
            "career_note": self.career_conf["note"],
            "industry": self.industry,
            "industry_label": self.industry_conf["label"],
            "volatility_pct": round(self.volatility * 100, 1),
            "buffer_months_required": self.buffer_months_required,
            "cap_multiple": round(self.career_multiple, 2),
            "ceiling_monthly": round(self.career_ceiling, 0),
            "mid_year": round(self.career_mid_year(), 1),
            "growth_pct_input": round(self.income_growth_rate * 100, 2),
            "nominal_curve": {str(y): round(self.salary_curve(y), 0) for y in years},
            "real_curve": {
                str(y): round(self.salary_curve(y) / ((1 + self.consumption_inflation) ** y), 0)
                for y in years
            },
            "nominal_30y_monthly": round(nominal_30, 0),
            "real_30y_monthly": round(nominal_30 / infl, 0),
            "real_30y_ratio": round(nominal_30 / infl / self.base_income, 3),
            "inflation_factor_30y": round(infl, 3),
            "note": (
                f"起薪 {self.base_income:,.0f} 元 → 天花板约 {self.career_ceiling:,.0f} 元"
                f"（{self.career_multiple:.2f} 倍，名义口径）；30 年后按今日物价约 "
                f"{nominal_30 / infl:,.0f} 元"
                if self.income_growth_rate > 0
                else f"你设定了 0% 增长：名义工资停在 {self.base_income:,.0f} 元，"
                f"30 年后按今日物价只剩 {self.base_income / infl:,.0f} 元"
            ),
        }

    def market_summary(self) -> Dict:
        return {
            "house_price_growth_pct": round(self.house_growth * 100, 2),
            "rent_growth_pct": round(self.rent_inflation * 100, 2),
            "inflation_pct": round(self.consumption_inflation * 100, 2),
            "note": "房价增速为 0 表示不做涨跌预测；租金与物价按输入复利",
        }

    def get_current_expense(self, year_idx: int) -> float:
        # 生活开销 + 隐形支出（红包/旅行/医疗/意外），一起随通胀膨胀
        base = self.month_base_exp + self.hidden_expense_month
        return base * ((1 + self.consumption_inflation) ** year_idx)

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

    def single_scenario_sim(self, rent_year: int, loan_year: int, down_strategy: str,
                            with_timeline: bool = False) -> Optional[Dict]:
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
        min_cash = self.init_saving
        min_buffer_months = float('inf')
        # 📊 V7.5 逐年明细（只在锚点方案要展示时收集，不拖慢 1800 组遍历）
        timeline_raw = [] if with_timeline else None
        month_raw = [] if with_timeline else None   # 完整 360 个月，前端按年下钻

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
            # 🏙️ 通勤交通费：租房期也要算进去，别把“住得远”当成免费的
            real_monthly_outflow = (
                self.get_current_expense(year_idx)
                + current_rent
                + car_cost
                + self.rent_transport_month
            )
            ratio = real_monthly_outflow / current_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio
            # 稳健模式不设租房硬顶（压力交给抗风险得分）；
            # 悬崖模式用 90% 生存线兜底，擦边通过。
            if self.rent_cap is not None and ratio > self.rent_cap:
                return None

            monthly_net = (current_income + current_fund) - real_monthly_outflow
            total_months_surplus_rate += monthly_net / current_income
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: return None
            remain_cash = max(0.0, remain_cash)
            if remain_cash < min_cash: min_cash = remain_cash
            if real_monthly_outflow > 1:
                min_buffer_months = min(min_buffer_months, remain_cash / real_monthly_outflow)
            if timeline_raw is not None:
                timeline_raw.append((year_idx, current_income, real_monthly_outflow,
                                     monthly_net, remain_cash, "rent"))
            if month_raw is not None:
                month_raw.append((t + 1, "rent", current_income, real_monthly_outflow,
                                  monthly_net, remain_cash))

        # 🏠 购房时点的房价：可选年增值（默认 0 = 不预测涨跌）
        purchase_price_yuan = self.house_total_yuan * ((1 + self.house_growth) ** rent_year)
        min_down_yuan = purchase_price_yuan * self.min_down_ratio
        purchase_tax_yuan = purchase_price_yuan * self.purchase_tax_rate
        total_upfront_needed = min_down_yuan + purchase_tax_yuan
        if remain_cash < total_upfront_needed - EPS_YUAN: return None

        actual_down_payment_yuan = 0.0
        if down_strategy == "min":
            actual_down_payment_yuan = min_down_yuan
        elif down_strategy == "max":
            actual_down_payment_yuan = min(remain_cash - purchase_tax_yuan, purchase_price_yuan)
        elif down_strategy == "balanced":
            extra = (remain_cash - total_upfront_needed) * 0.5
            actual_down_payment_yuan = min(min_down_yuan + extra, purchase_price_yuan)

        actual_loan_principal = purchase_price_yuan - actual_down_payment_yuan
        remain_cash -= actual_down_payment_yuan + purchase_tax_yuan
        remain_cash = max(0.0, remain_cash)

        # 🧱 V7.4 装修：毛坯房这笔钱在首付之后马上要花。
        #    不够也不枪毙方案——现实里很多人借钱装修、或者自己动手。
        #    记缺口，扣抗风险分，期末把借款扣回去。
        reno_total_yuan = self.reno_total_yuan()
        reno_gap_yuan = 0.0
        if reno_total_yuan > 0:
            if remain_cash >= reno_total_yuan:
                remain_cash -= reno_total_yuan
            else:
                reno_gap_yuan = reno_total_yuan - remain_cash
                remain_cash = 0.0

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
            
            real_monthly_outflow = self.get_current_expense(year_idx) + self.month_property + actual_out_of_pocket + car_cost + self.own_transport_month
            ratio = real_monthly_outflow / current_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio
            if real_monthly_outflow > (current_income * self.loan_cap): return None

            monthly_net = current_income - real_monthly_outflow
            total_months_surplus_rate += monthly_net / current_income
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            if remain_cash < -EPS_YUAN: return None
            remain_cash = max(0.0, remain_cash)
            if remain_cash < min_cash: min_cash = remain_cash
            if real_monthly_outflow > 1:
                min_buffer_months = min(min_buffer_months, remain_cash / real_monthly_outflow)
            if timeline_raw is not None:
                timeline_raw.append((year_idx, current_income, real_monthly_outflow,
                                     monthly_net, remain_cash, "loan"))
            if month_raw is not None:
                month_raw.append((current_idx + 1, "loan", current_income, real_monthly_outflow,
                                  monthly_net, remain_cash))

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
            
            real_monthly_outflow = self.get_current_expense(year_idx) + self.month_property + car_cost + self.own_transport_month
            ratio = real_monthly_outflow / current_income
            if ratio > max_outflow_ratio: max_outflow_ratio = ratio

            monthly_net = current_income - real_monthly_outflow
            total_months_surplus_rate += monthly_net / current_income
            remain_cash = remain_cash * (1 + self.month_invest_rate) + monthly_net
            remain_cash = max(0.0, remain_cash)
            if remain_cash < min_cash: min_cash = remain_cash
            if real_monthly_outflow > 1:
                min_buffer_months = min(min_buffer_months, remain_cash / real_monthly_outflow)
            if timeline_raw is not None:
                timeline_raw.append((year_idx, current_income, real_monthly_outflow,
                                     monthly_net, remain_cash, "free"))
            if month_raw is not None:
                month_raw.append((current_idx + 1, "free", current_income, real_monthly_outflow,
                                  monthly_net, remain_cash))

        safety_margin = 0.70 - max_outflow_ratio
        adapt_score = min(100.0, max(0.0, (safety_margin / 0.40) * 100))

        # 💨 现金缓冲：最低现金能覆盖几个月支出？覆盖不足按比例扣分
        if min_buffer_months == float("inf"):
            min_buffer_months = self.buffer_months_required
        buffer_score = min(1.0, min_buffer_months / max(self.buffer_months_required, 0.1))
        adapt_score = adapt_score * (0.55 + 0.45 * buffer_score)

        # 🧱 装修缺口：借钱装出来的房子，抗风险能力是要打折的（最高扣 35%）
        if reno_gap_yuan > 0:
            gap_ratio = min(1.0, reno_gap_yuan / max(reno_total_yuan, 1.0))
            adapt_score *= (1.0 - 0.35 * gap_ratio)
            # 借的钱终归要还：期末资产里扣回去（不制造"借来的钱也是资产"的幻觉）
            remain_cash = max(0.0, remain_cash - reno_gap_yuan)

        implied_rent_house = (self.base_rent * 12 / RENT_SALE_RATIO) / 10000
        target_house = self.req.house_total_wan
        rent_comfort = 82.0 if implied_rent_house >= target_house else 60.0 + 22.0 * (implied_rent_house / target_house)
        buy_comfort = 88.0
        occupied_years = SIM_TOTAL_YEARS - rent_year
        comfort_base = (rent_year * rent_comfort + occupied_years * buy_comfort) / SIM_TOTAL_YEARS

        # 🏙️ 空间居住体感：租房期看“这份租金在本地能租到什么”，买房期看“人均真实面积”
        rent_ratio_local = self.base_rent / self.local_2bed_rent
        rent_comfort_spatial = min(98.0, max(40.0, 45.0 + 45.0 * min(rent_ratio_local, 1.8)))
        own_month = SIM_TOTAL_YEARS * 12 - rent_month
        comfort_spatial_life = (
            rent_month * rent_comfort_spatial + own_month * self.own_comfort_spatial
        ) / (SIM_TOTAL_YEARS * 12)
        comfort_score = min(
            100.0,
            max(
                0.0,
                comfort_base * (1 - COMFORT_SPATIAL_WEIGHT)
                + comfort_spatial_life * COMFORT_SPATIAL_WEIGHT,
            ),
        )

        avg_surplus_rate = total_months_surplus_rate / (SIM_TOTAL_YEARS * 12)
        happiness_base = 100.0 if avg_surplus_rate >= 0.50 else max(0.0, (avg_surplus_rate / 0.50) * 100)
        # 相对收入位置：同样的结余，在平均薪资 1.4 万的城市和 7 千的城市，体感完全不同
        rel_mult = min(1.15, max(0.85, 0.85 + 0.15 * self.rel_income))
        happiness = min(100.0, happiness_base * rel_mult)

        # ⏱️ 时间效率：原有“租贷年限”得分 + 周期通勤得分（租房期/买房期分别计分）
        time_eff_raw = max(0.0, 100.0 - (rent_year / SIM_TOTAL_YEARS * 30 + loan_year / SIM_TOTAL_YEARS * 70))
        time_base = 60.0 + time_eff_raw * 0.4
        commute_life = (
            rent_month * self.rent_commute_score + own_month * self.own_commute_score
        ) / (SIM_TOTAL_YEARS * 12)
        time_eff = min(
            100.0,
            max(
                0.0,
                time_base * (1 - TIME_COMMUTE_WEIGHT) + commute_life * TIME_COMMUTE_WEIGHT,
            ),
        )

        months = None
        if month_raw is not None:
            months = [{
                "month": m,
                "phase": phase,
                "income": round(income, 0),
                "outflow": round(outflow, 0),
                "surplus": round(net, 0),
                "end_cash": round(cash, 0),
            } for m, phase, income, outflow, net, cash in month_raw]

        timeline = None
        if timeline_raw is not None:
            years = {}
            for year_idx, income, outflow, net, cash, phase in timeline_raw:
                y = years.setdefault(year_idx, {
                    "year": year_idx + 1, "phase": phase,
                    "income": 0.0, "outflow": 0.0, "net": 0.0, "end_cash": cash,
                })
                y["income"] += income
                y["outflow"] += outflow
                y["net"] += net
                y["end_cash"] = cash
                y["phase"] = phase
            timeline = [{
                "year": v["year"],
                "phase": v["phase"],
                "income_wan": round(v["income"] / 10000, 2),
                "outflow_wan": round(v["outflow"] / 10000, 2),
                "surplus_wan": round(v["net"] / 10000, 2),
                "end_cash_wan": round(v["end_cash"] / 10000, 2),
            } for v in sorted(years.values(), key=lambda x: x["year"])]

        return {
            "rent_year": rent_year, "loan_year": loan_year, "down_strategy": down_strategy,
            "occupied_years": round(occupied_years, 2),
            "down_payment_wan": round(actual_down_payment_yuan / 10000, 2),
            "loan_principal_wan": round(actual_loan_principal / 10000, 2),
            "liquid_asset_end_wan": round(remain_cash / 10000, 2),
            # 💸 名义资产 × 通胀折现 = 真实购买力，戳破“30年后千万富翁”的幻觉
            "real_asset_wan": round(
                remain_cash / ((1 + self.consumption_inflation) ** SIM_TOTAL_YEARS) / 10000, 2
            ),
            "min_cash_wan": round(min_cash / 10000, 2),
            "buffer_months": round(min_buffer_months, 2),
            "buffer_required": self.buffer_months_required,
            "purchase_price_wan": round(purchase_price_yuan / 10000, 2),
            # 💸 总利息：用户凭直觉做判断时，脑子里算的就是这个数
            "total_interest_wan": round((sum(loan_schedule) - actual_loan_principal) / 10000, 2),
            "reno_budget_wan": round(reno_total_yuan / 10000, 2),
            "reno_gap_wan": round(reno_gap_yuan / 10000, 2),
            "reno_unit_sqm": round(self.reno_unit_sqm, 0),
            "timeline": timeline,
            "months": months,
            "max_deficit_wan": round(max_deficit / 10000, 2),
            "comfort_spatial": round(comfort_spatial_life, 2),
            "rent_comfort_spatial": round(rent_comfort_spatial, 2),
            "commute_lifecycle": round(commute_life, 2),
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


# ⚡ 速度型：四维都及格的前提下，谁最早"完成任务"（租房 + 还贷 年限最短）
SPEED_PASS = 60.0
SPEED_RELAX = 50.0


def build_style_boards(out_list: List[Dict]) -> Dict[str, List[Dict]]:
    """风格看板。

    · 稳健型：抗风险分最高（首付不激进，留缓冲）
    · 速度型：四项（抗风险/舒适/幸福/时间）都过及格线，然后按"租+贷 总年限"最短排——
      它的含义是"别为了快把别的维度搞崩"，而不是"越快越好"
    · 进取型：最低首付、把现金留在理财，看期末资产
    """
    dims = ("adapt_score", "comfort_score", "surplus_happiness", "time_efficiency")

    def all_pass(x, line):
        return all(x[d] >= line for d in dims)

    qualified = [x for x in out_list if all_pass(x, SPEED_PASS)]
    if not qualified:
        qualified = [x for x in out_list if sum(x[d] >= SPEED_RELAX for d in dims) >= 3]
    if not qualified:
        qualified = out_list

    return {
        "稳健型": sorted(
            [x for x in out_list if x["down_strategy"] in ("max", "balanced")],
            key=lambda x: x["adapt_score"],
            reverse=True,
        )[:STYLE_TOP_N],
        "速度型": sorted(
            qualified,
            key=lambda x: (x["rent_year"] + x["loan_year"], -x["comprehensive_score"]),
        )[:STYLE_TOP_N],
        "进取型": sorted(
            [x for x in out_list if x["down_strategy"] == "min"],
            key=lambda x: x["liquid_asset_end_wan"],
            reverse=True,
        )[:STYLE_TOP_N],
    }


def _log_run(req, summary: Dict) -> None:
    """推演留痕：失败不影响主流程。"""
    if not (data_layer and data_layer.enabled()):
        return
    try:
        data_layer.save_run(getattr(req, "city_code", None), req.model_dump(), summary)
    except Exception as e:  # noqa: BLE001
        logging.warning("推演留痕失败：%s", e)


# =====================================================================
# 🚀 FastAPI 接口
# =====================================================================
app = FastAPI(title="房产金融WebGIS租购决策仿真系统 V7.0 · PostgreSQL/PostGIS")
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
    spatial_payload = engine.spatial_summary()
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

    if out_list:
        _log_run(req, {
            "mode": "relative",
            "feasible": len(out_list),
            "top": {k: out_list[0].get(k) for k in (
                "rent_year", "loan_year", "down_strategy",
                "liquid_asset_end_wan", "real_asset_wan", "comprehensive_score")},
        })

    return {
        "code": 200,
        "mode": "relative",
        "warning": warning,
        "assumptions": MODEL_ASSUMPTIONS,
        "spatial": spatial_payload,
        "risk": engine.risk_summary(),
        "career": engine.career_summary(),
        "market": engine.market_summary(),
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
    spatial_payload = engine.spatial_summary()

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

    if out_list:
        _log_run(req, {
            "mode": "absolute",
            "feasible": len(out_list),
            "top": {k: out_list[0].get(k) for k in (
                "rent_year", "loan_year", "down_strategy", "rent_level",
                "liquid_asset_end_wan", "real_asset_wan", "comprehensive_score")},
        })

    return {
        "code": 200,
        "mode": "absolute",
        "warning": warning,
        "assumptions": MODEL_ASSUMPTIONS,
        "spatial": spatial_payload,
        "risk": engine.risk_summary(),
        "career": engine.career_summary(),
        "market": engine.market_summary(),
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



# =====================================================================
# 🧭 V7.5 锚点方案：让用户先验算自己的打算
# ---------------------------------------------------------------------
# 信任不是靠"我算得准"这句话建立的，是靠"我的方案你算得跟我心里一样"建立的。
# 所以：用户报出他打算怎么买（租几年、贷几年、首付怎么付），我们原样算一遍，
# 把逐年明细摊开给他对账；对得上，他才有理由看我们推荐的那个。
# =====================================================================
class AnchorRequest(SimulationRequest):
    anchor_rent_year: int = Field(ge=0, le=SIM_TOTAL_YEARS, default=5)
    anchor_loan_year: int = Field(ge=1, le=SIM_TOTAL_YEARS, default=10)
    anchor_down_strategy: str = Field(default="balanced")


def score_against_reference(scheme: Dict, reference: List[Dict], ew: Dict) -> Dict:
    """用全量方案的极差给单个方案打分，保证和排名里的分数可比。"""
    item = dict(scheme)
    mins = {k: min(x[f] for x in reference) for k, f in ENTROPY_INDICATORS}
    maxs = {k: max(x[f] for x in reference) for k, f in ENTROPY_INDICATORS}
    comp = 0.0
    for k, f in ENTROPY_INDICATORS:
        span = maxs[k] - mins[k]
        if span < EPS_FLOAT:
            norm_val = 0.0
        else:
            v = min(max(item[f], mins[k]), maxs[k])
            norm_val = (v - mins[k]) / span
        comp += norm_val * ew[f"weight_{k}"]
    item["comprehensive_score"] = round(comp * 100, 2)
    item["suitability_score"] = round(
        item["comfort_score"] * SUITABILITY_WEIGHTS["comfort"]
        + item["adapt_score"] * SUITABILITY_WEIGHTS["adapt"]
        + item["surplus_happiness"] * SUITABILITY_WEIGHTS["happiness"]
        + item["time_efficiency"] * SUITABILITY_WEIGHTS["time"], 2)
    return item


@app.post("/api/simulate/anchor")
def simulate_with_anchor(req: AnchorRequest):
    """先跑全量，再把用户自己的组合单独算一遍，返回对比与逐年明细。"""
    base = simulate_relative(req)
    all_scheme = base["all_scheme"]
    ew = base["entropy_result"]

    engine = HousingFinanceEngine(req)
    ry = int(req.anchor_rent_year)
    ly = int(req.anchor_loan_year)
    ds = req.anchor_down_strategy if req.anchor_down_strategy in DOWN_STRATEGIES else "balanced"

    LOAN_SCHEDULE_CACHE.clear()
    raw = engine.single_scenario_sim(ry, ly, ds, with_timeline=True)

    if raw is None:
        # 不可行：不糊弄，给出同一租房年限下最接近的可选项
        same_rent = [x for x in all_scheme if x["rent_year"] == ry]
        if not same_rent:
            # 用户报的年限超出可行域（例如租 25 年，但模拟只有 30 年）
            same_rent = all_scheme
        near = sorted(
            same_rent,
            key=lambda x: (abs(x["rent_year"] - ry) * 100 + abs(x["loan_year"] - ly),
                           -x["comprehensive_score"]),
        )[:3]
        result = dict(base)
        result.update({
            "mode": "anchor",
            "feasible": False,
            "anchor_request": {"rent_year": ry, "loan_year": ly, "down_strategy": ds},
            "reason": "这个组合在你的收入、首付和支出下过不了风控：要么首付+税费凑不齐，要么还贷期支出超过收入红线。",
            "near_feasible": near,
        })
        return result

    anchor = score_against_reference(raw, all_scheme, ew)
    best = base["top_comprehensive"][0] if base["top_comprehensive"] else anchor

    # 推荐方案也要一份逐月/逐年明细，前端才能做左右对比
    best_raw = engine.single_scenario_sim(
        best["rent_year"], best["loan_year"], best["down_strategy"], with_timeline=True
    ) if best else None

    # 排名：在全部可行方案里的位次
    rank = 1 + sum(1 for x in all_scheme if x["comprehensive_score"] > anchor["comprehensive_score"])

    delta = {
        "asset_wan": round(best["liquid_asset_end_wan"] - anchor["liquid_asset_end_wan"], 2),
        "real_asset_wan": round(best["real_asset_wan"] - anchor["real_asset_wan"], 2),
        "interest_wan": round(best.get("total_interest_wan", 0) - anchor.get("total_interest_wan", 0), 2),
        "comprehensive": round(best["comprehensive_score"] - anchor["comprehensive_score"], 2),
        "risk_score": round(best["risk_score"] - anchor["risk_score"], 2),
        "buffer_months": round(best["buffer_months"] - anchor["buffer_months"], 2),
    }

    # ⚠️ 以全量结果为底再覆盖：style_boards / top_* 这些字段一个都不能漏，
    #    漏了前端三个风格 tab 就是空的（V7.7 的 bug 就出在这里）
    result = dict(base)
    result.update({
        "mode": "anchor",
        "feasible": True,
        "anchor_request": {"rent_year": ry, "loan_year": ly, "down_strategy": ds},
        "anchor": anchor,
        "best": best,
        "rank": rank,
        "total": len(all_scheme),
        "delta": delta,
        "timeline": anchor.get("timeline"),
        "best_months": (best_raw or {}).get("months"),
        "best_timeline": (best_raw or {}).get("timeline"),
    })
    return result


@app.get("/api/cities")
def api_cities():
    """V7.0：城市列表。数据库不可用时返回空，前端自动回退到内置 CITY_DB。"""
    if data_layer and data_layer.enabled():
        try:
            return {"code": 200, "data": data_layer.list_cities()}
        except Exception as e:  # noqa: BLE001
            logging.warning("读取 cities 失败：%s", e)
    return {"code": 200, "data": [], "fallback": True}


@app.get("/api/cities/{city_code}/districts")
def api_districts(city_code: str):
    if data_layer and data_layer.enabled():
        try:
            return {"code": 200, "data": data_layer.list_districts(city_code)}
        except Exception as e:  # noqa: BLE001
            logging.warning("读取 districts 失败：%s", e)
    if city_code == "450100":
        return {"code": 200, "data": NANNING_REGIONS, "fallback": True}
    return {"code": 200, "data": [], "fallback": True}


@app.get("/api/listings")
def api_listings(
    city_code: Optional[str] = None,
    max_price_wan: Optional[float] = None,
    min_area: Optional[float] = None,
    bedrooms: Optional[int] = None,
    district_id: Optional[int] = None,
    limit: int = 50,
):
    """V7.1：房源列表。没库、没数据、或查询失败都返回空数组 + 原因，绝不编造。"""
    if not (data_layer and data_layer.enabled()):
        return {"code": 200, "data": [], "fallback": True, "reason": "数据库未启用"}
    try:
        rows = data_layer.list_listings(
            city_code=city_code,
            max_price=(max_price_wan * 10000 if max_price_wan is not None else None),
            min_area=min_area,
            bedrooms=bedrooms,
            district_id=district_id,
            limit=limit,
        )
        return {"code": 200, "data": rows, "count": len(rows)}
    except Exception as e:  # noqa: BLE001
        logging.warning("读取 listings 失败：%s", e)
        return {"code": 200, "data": [], "fallback": True, "reason": str(e)}


@app.get("/api/listings/stats")
def api_listing_stats(city_code: Optional[str] = None):
    """V7.1：按城市汇总房源数与价格区间。空表就是空数组，用来诚实显示"暂无数据"。"""
    if not (data_layer and data_layer.enabled()):
        return {"code": 200, "data": [], "fallback": True, "reason": "数据库未启用"}
    try:
        return {"code": 200, "data": data_layer.listing_stats(city_code=city_code)}
    except Exception as e:  # noqa: BLE001
        logging.warning("统计 listings 失败：%s", e)
        return {"code": 200, "data": [], "fallback": True, "reason": str(e)}


@app.get("/api/health/db")
def api_db_health():
    if not data_layer:
        return {"enabled": False, "reason": "db module not importable"}
    return data_layer.health()


@app.get("/api/gis/regions")
def get_regions():
    if data_layer and data_layer.enabled():
        try:
            rows = data_layer.list_districts("450100")
            if rows:
                data = [{
                    "name": r["name"], "lat": r["lat"], "lng": r["lng"],
                    "price_sqm": r["price_sqm"], "rent_ref": r["rent_ref"],
                    "commute_min": r["commute_min"],
                    "house_ref": round(r["price_sqm"] * 90 / 10000),
                    "desc": "数据库片区",
                } for r in rows]
                return {"code": 200, "data": data, "source": "db"}
        except Exception as e:  # noqa: BLE001
            logging.warning("读取南宁片区失败：%s", e)
    return {"code": 200, "data": NANNING_REGIONS, "source": "fallback"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

















