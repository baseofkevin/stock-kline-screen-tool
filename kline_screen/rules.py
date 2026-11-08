"""启动日定位、逐条裁决与剔除原因归一。"""
import re

from .common.values import fnum
from .params import CHG_HI, CHG_LO, MIN_HISTORY


def ma(v, n, i):
    return sum(v[i - n + 1:i + 1]) / n if i >= n - 1 else None


def find_start(code, name, bars, S):
    """在 S 日回溯该股的启动日 d*。失败时返回 (None, 原因)"""
    if not bars:
        return None, "无K线数据"
    i = next((j for j, x in enumerate(bars) if x[0] == S), None)
    if i is None:
        return None, f"K线无 {S} 数据"
    if i < MIN_HISTORY:
        # i 是 S 的 0 基下标，bars[:i+1] 实际有 i+1 根。
        return None, f"K线历史不足({i + 1}根，需≥{MIN_HISTORY + 1})"
    cl = [x[1] for x in bars]
    vo = [x[2] for x in bars]
    up = 0
    for j in range(i, 19, -1):
        m = ma(cl, 20, j)
        if m and cl[j] > m:
            up += 1
        else:
            break
    if up == 0:
        return None, "未站上 MA20"
    ci = i - (up - 1)
    # 回扫最多到索引 20（i≥70 时约 51 天），若股票连续更久站在 MA20 上方，
    # ci 会停在回扫边界而不是真正的上穿日。规则文档 §2.1 ① 要求
    # 「收(d*−1) ≤ MA20(d*−1) 且 收(d*) > MA20(d*)」，这里补齐前半句的断言。
    # 正常回扫停下时 cl[ci-1] 必然 ≤ MA20(ci-1)；只有触及边界才会不满足。
    if ci > 0:
        mprev = ma(cl, 20, ci - 1)
        if mprev is not None and cl[ci - 1] > mprev:
            return None, "非真上穿（连续站上 MA20 超过回扫上限）"
    return {"code": code, "name": name, "d_star": bars[ci][0], "age": i - ci,
            "close": cl[i], "ma20": ma(cl, 20, i), "ma60": ma(cl, 60, i),
            "cl_s": cl[ci], "m20s": ma(cl, 20, ci), "m60s": ma(cl, 60, ci),
            "vcross": (vo[ci] / (sum(vo[ci - 5:ci]) / 5)
                       if ci >= 5 and sum(vo[ci - 5:ci]) else 0),
            "_ci": ci, "_i": i}, ""


def judge(det, bars, S, N, cross_by_day, st_by_day, new_by_day,
          k_vol, gap, c2, c3, chg_lo=CHG_LO, chg_hi=CHG_HI):
    c = det["code"]
    d0 = det["d_star"]
    if det["age"] > N:
        return False, f"⑦ 距启动{det['age']}日 > N={N}"
    if det["vcross"] < k_vol:
        return False, f"② 启动日放量不足({det['vcross']:.2f}<{k_vol})"
    # age 是「该股自己的 K 线索引差」，不是市场交易日差。若窗口内停牌，
    # d* 的实际日期可能早于 need[0]，而 cross_by_day 只覆盖 need → 这里
    # 会被误记为「日频条件不通过」。单独给一个原因，避免污染追溯信息。
    if d0 not in cross_by_day:
        return False, f"③ 启动日({d0})不在本次覆盖窗口（该股存在停牌）"
    row0 = cross_by_day[d0].get(c)
    if row0 is None:
        return False, f"③ 启动日({d0})未通过日频条件"
    # 服务端条件必须在本地复核。否则缓存参数不一致/字段缺失时，
    # 会静默放进不符合规则的股票。
    chg = fnum(row0.get("ChangePCT"))
    if chg is None:
        return False, "③ 启动日涨幅字段缺失"
    if not (chg_lo - 1e-9 <= chg <= chg_hi + 1e-9):
        return False, f"③ 启动日涨幅{chg:.2f}%不在[{chg_lo},{chg_hi}]"
    # v1.3 起净流入只作展示、不作裁决。字段是否返回由 main() 的
    # 「横截面字段缺失」防线统一保证（服务端只返回表达式引用到的列）。
    for flag in ("IsDelisted", "Ifsuspend"):
        v = fnum(row0.get(flag))
        if v is not None and v != 0:
            return False, f"③ 启动日 {flag}≠0"
    if det["m20s"] is None or det["m60s"] is None:
        return False, "④ 启动日无均线数据"
    if det["m20s"] < det["m60s"] * c3:
        return False, f"④ 启动日 MA20 < MA60×{c3}"
    cl = [x[1] for x in bars]
    if c2 is not None:
        # MA20(d*−3) 需要 (ci−3) ≥ 19，即 ci ≥ 22；find_start 保证 ci ≥ 20。
        if det["_ci"] < 22:
            return False, "④ 启动日 MA20 3日斜率数据不足"
        m20s3 = ma(cl, 20, det["_ci"] - 3)
        if det["m20s"] <= m20s3 * (1 - c2):
            return False, f"④ 启动日 MA20 近3日跌逾{c2*100}%"
    if gap and det["cl_s"] / det["m60s"] >= gap:
        return False, f"④ 启动日 距MA60 ≥ {gap}"
    if c in st_by_day.get(d0, set()):
        return False, "⑤ ST"
    if c in new_by_day.get(d0, set()):
        return False, "⑤ 次新（未满3年）"
    # d* 当天不是 ST/次新，不代表 S 日也不是。S 一定在 need 里，
    # 这两个集合已取到，复检成本为零；否则「d* 之后被 ST」的股票仍会入选。
    if c in st_by_day.get(S, set()):
        return False, "⑤ S日 ST"
    if c in new_by_day.get(S, set()):
        return False, "⑤ S日 次新（未满3年）"
    # ⑥「启动后未跌破 MA20」是**定义性条件**（规则文档 §2.2）：d* 的定位方式
    # 保证 [ci, i] 每天都在 MA20 上方，所以本循环恒不触发、剔除数恒为 0。
    # 保留它只作防御性断言，不是独立风控；真正想防的「回踩破启动日低点」需先
    # 重下带 OHLC 的历史 K 线（旧缓存是 3 元组，见 bar_at 注释）。
    for j in range(det["_ci"] + 1, det["_i"] + 1):
        m = ma(cl, 20, j)
        if not m or cl[j] <= m:
            return False, "⑥ 启动后跌破 MA20"
    return True, ""


REJ_STAGE = {"FS": "find_start 定位启动日",
             "JD": "judge 逐条裁决",
             "MV": "市值比二次过滤（已废弃）",
             "TN": "20日换手分位过滤"}


def reason_cat(why):
    """把剔除原因归一成**聚合键**：可变数字 → #，固定均线周期放行。

    这只用于「剔除统计」的顶层分组（例如把
    `⑦ 距启动4日 > N=4`/`…5日…`/`…6日…` 合成 `⑦ 距启动#日 > N=#` 一行），
    **不得**当作唯一账本。真实参数（N、量能比、age、日期）保留在
    「剔除明细」的原文键里，逐股对应关系保留在「剔除代码」里，否则历史 JSON
    无法反推当时阈值，也无法回答「某只股票为什么没选上」。

    `MA20`/`MA60` 是写死在 find_start()/judge() 里的固定周期，不是可变参数，
    一并抹成 `MA#` 只会让统计表读不出是哪条均线（如「未站上 MA#」），
    因此放行 `MA\\d+`；其余数字仍归一（`N=4`、`1.20<1.75`、`0.2%` 等确实会变）。
    """
    return re.sub(r"MA\d+|[\d.]+",
                  lambda m: m.group(0) if m.group(0).startswith("MA") else "#",
                  why)
