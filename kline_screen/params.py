"""规则参数、表达式与输出列定义。"""

# ---------------- 规则参数（见 README.md「参数」一节） ----------------
K_VOL = 1.5           # ② 启动日放量倍数
CHG_LO, CHG_HI = 0.5, 20.5    # ③ 当日涨幅区间 %
C3_RATIO = 0.95       # ④ MA20 >= MA60 × 该值（v1.4 由 0.98 放宽）
C2_TOL = 0.01         # ④ MA20 近3日跌幅容忍度（实测 0.005 偏紧）
MA60_GAP = 0.0        # 条件9 close/MA60 上限；0 = 关闭
KLINE_BATCH = 20            # 单批只数（服务端吞吐约 600 只/分钟，20 只/批贴着这个上限最稳）
KLINE_FLUSH_EVERY = 20      # 每处理这么多批落盘一次（0 = 只在全部拉完后落盘）
KLINE_RETRY_BATCH = 20      # 补取 / 截断重取时的每批只数
KLINE_PACE_MIN = 0.2        # 批间最小间隔（秒）；完整返回时逐步降到这个值
KLINE_PACE_MAX = 3.0        # 批间最大间隔（秒）；被限频时逐步升到这个值
KLINE_BAD_LIMIT = 3         # 连续这么多批整体失败 → 判接口不可用，中止
KLINE_DELTA_BATCH = 60      # 增量补新（每只只回几天）时的每批只数
KLINE_MAX_BARS = 300        # 每只最多保留的根数（≈14 个月），防止缓存逐年膨胀
LABEL_PAGE = 500
KLINE_LOOKBACK_DAYS = 300   # K线回溯长度（自然日），保证 MA60 与启动日检测有足够历史
TURN_WINDOW = 20      # 换手率统计窗口（交易日）
TURN_MIN_COVER = 15   # 窗口内至少覆盖这么多天，否则视为数据不足
TURN_PCT = 0.0        # 换手默认只排序标注、不剔除；设 0.4 = 硬过滤保留池内最低 40%
TURN_MIN_N = 5        # 名单少于该数 → 自动放宽到 60%
TURN_MAX_N = 30       # 名单多于该数 → 自动收紧到 20%

MIN_HISTORY = 70      # find_start 要求基准日 S 的索引 i ≥ 70（即含当日共 ≥71 根）


# ⚠️ CROSS_TPL 一旦改动，必须同时把 CROSS_CACHE_VERSION +1。
#    否则旧 cross_*.json 会被当成当前表达式的结果复用（服务端字段随表达式来去）。
CROSS_CACHE_VERSION = 3

# ---------------- 财务数据取数（供业绩转变标注） ----------------
FIN_PERIODS = 20             # data_finance 拉取期数（5 年）
FIN_BATCH = 10               # data_finance 单批股票数（利润表）

# ---------------- 业绩转变标注（只标注，不参与任何裁决） ----------------
# 定位：在基础行情列之后再加三组「判断 + 证据」列，回答「有没有出现业绩拐点」。
#       三个指标**各自独立判断**，不合计总分、不合成「是/部分/否」总标签，
#       同样**不筛选、不排序、不影响入选名单**：
#   ① 单季毛利率最近 2 个季度严格递增（GM[t-1] < GM[t]）
#      + 最新一季营收环比不为负 + 最新单季高于上年同季
#   ② 合同负债+预收款项 同比 ≥20%，且 ÷ 营业总收入TTM ≥2%
#   ③ 经营现金流TTM >0，且（由负转正 或 归母净利润TTM ≤0 或 OCF/NP >1）
TURN_GM_REQUIRE_YOY = True   # ① 同比护栏：最新单季毛利率须高于上年同季
TURN_CL_YOY_MIN = 20.0       # ② 订单代理同比下限（%）
TURN_CL_REV_MIN = 2.0        # ② 订单代理 / 营业总收入TTM 下限（%）
TURN_OCF_NP_MIN = 1.0        # ③ 经营现金流TTM / 归母净利润TTM 下限
TURN_FIN_PERIODS = 20        # data_finance 拉取期数（5 年，足够看 2 季 + 同比）
TURN_FIN_BATCH = 5           # 限频明显：单批 5 只，比利润表的 10 只更稳
TURN_FIN_SLEEP = 2.5         # 批间休眠（秒）
TURN_FIN_RETRIES = 6         # 单批重试次数
TURN_FIN_BACKOFF = 15.0      # 限频退避（秒）；普通失败按 3s×次数退避

# ---------------- 财务健康提示（只提示，不参与任何裁决） ----------------
# 四项：现金短债比、净现比、应收增速差、速动比率。
# 现金短债比与速动比率取最新报告期；净现比、应收增速差看最近 2 个年报。
HEALTH_CASH_DEBT_WARN = 1.0      # 现金短债比 <1.0 紧张
HEALTH_CASH_DEBT_DANGER = 0.5    # <0.5 高危
HEALTH_OCF_NP_WARN = 0.5         # 净现比连续 2 年 <0.5（或为负）
HEALTH_AR_GAP_WARN = 15.0        # 应收增速差连续 2 年 >+15pct
HEALTH_QUICK_WARN = 0.7          # 速动比率 <0.7
HEALTH_ANNUAL_YEARS = 2          # “连续 N 年”的年报数

# ---------------- 估值列（只展示，不参与任何裁决） ----------------
# DCF 口径与 ~/MyData/stock-quality-screen-tool/METHODOLOGY.md §5 对齐：
#   FCF0 = 最近年报自由现金流；g1 = 5 年营收 CAGR（夹在 0~15%）
#   r = 9% 固定折现率；g = 2.5% 永续增速；n = 10 年
#   EV = Σ FCF0(1+g1)^t/(1+r)^t + FCF0(1+g1)^n(1+g)/(r-g)/(1+r)^n
#   合理价 = EV / 总股本；空间 = (合理价 - 现价) / 合理价
# ⚠️ 工具用的是「经营现金流 - 资本开支」，而 westock 财报没有资本开支字段，
#    这里用同一张表的 FCFF（企业自由现金流，已是扣资本开支的口径）代替。
DCF_R = 0.09                 # 固定折现率（WACC）
DCF_G = 0.025                # 永续增速
DCF_N = 10                   # 第一阶段年数
DCF_G1_CAP = 0.15            # 营收 CAGR 上限
DCF_MIN_ANNUAL = 5           # 至少要这么多年报才算得出来
FIN_TYPES = ("银行", "保险", "证券")   # 金融股不适用（FCF 口径无意义）

# ⚠️ 表达式里的 `MainNetFlow > -1e15` / `MainNetFlow5D > -1e15` 是**恒真条件**，
#    唯一作用是「字段请求」——服务端**只返回表达式引用到的列**。删掉它们，
#    返回表里就没有 MainNetFlow / MainNetFlow5D，本地市值比展示列全部算不出来，
#    main() 的字段防线会直接报错；若连防线一起删，名单会被静默清空。
#    v1.3 起净流入**不再参与筛选**，只作展示；-1e15 远低于任何真实净流入
#    （A 股单日主力净流入量级约 ±1e11），因此不会误杀。
#    同理，任何字段（ChangePCT / MainNetFlow5D …）一旦不再被引用就会从返回里消失。
#    改动本表达式后必须重跑验证；main() 里有对应防线会直接报错。
CROSS_TPL = ("intersect([ChangePCT >= {lo}, ChangePCT <= {hi}, "
             "MainNetFlow > -1000000000000000, "
             "MainNetFlow5D > -1000000000000000, "
             "IsDelisted == 0, Ifsuspend == 0])")
NEW_LABELS = ["listeddate_5days", "listeddate_3mons",
              "listeddate_1year", "listeddate_3year"]

CSV_HEADER = ["代码", "名称", "一级行业", "二级行业", "启动日", "距启动交易日",
              "启动日量能倍数", "启动日涨幅%", "距MA20%", "距MA60%",
              "毛利率改善", "订单增长", "经营现金流情况", "财务健康提示",
              "20日换手%", "池内换手分位", "分位星级",
              "最新收盘价", "PE", "PB", "PS",
              "DCF合理价", "DCF空间%", "DCF说明"]
