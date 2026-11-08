"""CSV 行组装、result JSON 与终端摘要。"""
import os

from .common.values import r2
from .params import (C2_TOL, C3_RATIO, HEALTH_ANNUAL_YEARS,
                     HEALTH_AR_GAP_WARN, HEALTH_CASH_DEBT_DANGER,
                     HEALTH_CASH_DEBT_WARN, HEALTH_OCF_NP_WARN,
                     HEALTH_QUICK_WARN, TURN_CL_REV_MIN, TURN_CL_YOY_MIN,
                     TURN_GM_REQUIRE_YOY, TURN_OCF_NP_MIN)
from .rules import REJ_STAGE


def turn_star(rank):
    """换手分位的星级标记（只影响展示，不参与排序/裁决）。

    Q1（0 ≤ rank < 20）→ `***`，Q2（20 ≤ rank < 40）→ `**`，
    Q3（40 ≤ rank < 60）→ `*`；Q4/Q5 与分位为空（数据不足）→ 留空。
    边界与 evaluate.py 的五等分口径一致（20 归 Q2、40 归 Q3）。
    """
    if rank is None:
        return ""
    if rank < 20:
        return "***"
    if rank < 40:
        return "**"
    if rank < 60:
        return "*"
    return ""


def turn_cell(label, desc):
    """把独立判断与证据拼成一格：是/否/无数据 + 全角冒号 + 证据。"""
    if not label:
        return ""
    if not desc or label == "不适用":
        return label
    return f"{label}：{desc}"


def csv_row(x):
    """一只入选股的 CSV 行，顺序与 CSV_HEADER 严格对应（20 列）。"""
    return [
        x["code"], x.get("name") or "",
        x.get("sw1") or "未分类", x.get("sw2") or "未分类",
        x["d_star"], x["age"], round(x["vcross"], 2), x.get("chg"),
        x["r20"], x["r60"],
        turn_cell(x.get("turn_p1", ""), x.get("turn_gm", "")),
        turn_cell(x.get("turn_p3", ""), x.get("turn_ocf", "")),
        x.get("health_text", ""),
        x.get("turn20"), x.get("turn20_rank"),
        turn_star(x.get("turn20_rank")),
        r2(x.get("price")), r2(x.get("pe")), r2(x.get("pb")),
        r2(x.get("ps")),
    ]


def build_result(st):
    """组装 result_<S>.json 的完整运行明细。"""
    a = st["a"]
    turn = st["turn"]
    csv_path, res_path = st["csv_path"], st["res_path"]
    stats = dict(sorted(st["rej"].items(), key=lambda kv: -kv[1]))
    return {
        "版本": "v1.8",
        "基准日": st["S"], "扫描周期K": a.n + 1, "新鲜度窗口N": a.n,
        "覆盖区间": st["need"],
        "参数": {"K_VOL": a.k_vol, "涨幅区间": [a.chg_lo, a.chg_hi],
                 "MA60上限": a.ma60_gap,
                 "换手过滤": st["turn_meta"],
                 "市值比": "仅展示，不参与裁决（v1.1 起）",
                 "C2容忍": C2_TOL, "C3比例": C3_RATIO,
                 "次新股口径": "服务端标签四桶并集"
                               "（listeddate_5days/3mons/1year/3year）",
                 "市值比分母": "d* 日流通市值（与分子同日）",
                 "市值比分母来源": "mv_<d*>.json（screen condition NegotiableMV）",
                 "行业归属": "申万一级/二级，来源 westock data_sector；"
                             "只展示，不参与裁决（v1.7 起）"},
        "业绩转变参数": {"判定方式": "三项独立判断，不合计总分",
                         "①毛利率环比季数": 2,
                         "①营收环比不为负": True,
                         "①最新单季毛利率高于上年同季": TURN_GM_REQUIRE_YOY,
                         "②合同负债+预收同比下限%": TURN_CL_YOY_MIN,
                         "②订单代理字段": "ContractLiability+AdvanceReceipts",
                         "②订单/营收TTM下限%": TURN_CL_REV_MIN,
                         "③经营现金流TTM下限": 0,
                         "③现金流/净利润下限": TURN_OCF_NP_MIN,
                         "③OCF由负转正算通过": True,
                         "③亏损但OCF为正算通过": True,
                         "数据是否可用": turn is not None,
                         "缺失代码": st["turn_failed"]},
        "财务健康参数": {
            "现金短债比紧张阈值": HEALTH_CASH_DEBT_WARN,
            "现金短债比高危阈值": HEALTH_CASH_DEBT_DANGER,
            "净现比下限": HEALTH_OCF_NP_WARN,
            "净现比连续年数": HEALTH_ANNUAL_YEARS,
            "应收增速差上限pct": HEALTH_AR_GAP_WARN,
            "应收增速差连续年数": HEALTH_ANNUAL_YEARS,
            "速动比率下限": HEALTH_QUICK_WARN,
            "货币资金口径": "CashEquivalents（接口无受限资金字段，未剔受限）",
            "应收口径": "BillAccReceivable",
            "短期有息负债口径": "ShortTermLoan + NonCurrentLiabilityIn1Year"},
        "粗筛池": st["pool_n"], "入选数": len(st["final"]),
        # 剔除非（兼容旧字段）/ 剔除统计 为同一份归一化聚合
        "剔除非": stats,
        "剔除统计": stats,
        "剔除明细": dict(sorted(st["rej_raw"].items(), key=lambda kv: -kv[1])),
        "剔除代码": st["rej_code"],
        "剔除阶段": REJ_STAGE,
        "剔除口径": ("统计=数字归一化聚合（仅用于分组展示）；"
                     "明细=原文，保留真实 N/量能比/age/日期；"
                     "代码=逐股去向 [阶段码, 原因]"),
        "产物": {"csv": os.path.basename(csv_path), "csv行数": st["n_csv"],
                 "json": os.path.basename(res_path)},
        "名单": st["final"],
    }


def print_summary(st):
    """终端摘要：剔除统计、行业分布、名单表与产物路径。"""
    final = st["final"]
    print(f"\n粗筛池 {st['pool_n']} 只 → 入选 {len(final)} 只\n")
    print("剔除明细（按原因聚合；逐股原因见 result JSON 的「剔除代码」）：")
    for k, v in sorted(st["rej"].items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {v}")
    if final:
        cnt = {}
        for x in final:
            k = x.get("sw1") or "未分类"
            cnt[k] = cnt.get(k, 0) + 1
        print("\n行业分布（申万一级，只展示、不参与裁决）：" + " ｜ ".join(
            f"{k} {v} 只"
            for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))))

        def _mark(v):
            return {"是": "是", "否": "否", "无数据": "无",
                    "不适用": "不"}.get(v, "-")
        print(f"\n{'代码':10s}{'名称':9s}{'启动日':12s}{'距启动':>6s}"
              f"{'量能':>7s}{'换手%':>7s}{'池内分位':>7s}"
              f"{'距MA20':>8s}{'距MA60':>8s}{'市值比':>8s}"
              f"{'流通市值亿':>11s}{'PE':>10s}"
              f"{'①':>3s}{'②':>3s}{'③':>3s}")
        for x in final:
            t20 = f"{x['turn20']:7.2f}" if x.get("turn20") is not None else "      -"
            tr = (f"{x['turn20_rank']:7.1f}"
                  if x.get("turn20_rank") is not None else "      -")
            print(f"{x['code']:10s}{(x.get('name') or '')[:8]:9s}"
                  f"{x['d_star']:12s}{x['age']:6d}{x['vcross']:7.2f}"
                  f"{t20}{tr}"
                  f"{x['r20']:8.2f}{x['r60']:8.2f}"
                  f"{(x.get('flow_ratio') or 0):7.2f}%"
                  f"{str(x.get('float_cap_yi') or '-'):>11s}"
                  f"{str(x.get('pe') or '-'):>10s}"
                  f"{_mark(x.get('turn_p1')):>3s}"
                  f"{_mark(x.get('turn_p2')):>3s}"
                  f"{_mark(x.get('turn_p3')):>3s}")
    print(f"\nCSV    : {st['csv_path']}")
    print(f"明细   : {st['res_path']}")
    print(f"原始数据: {st['DB']}/")
