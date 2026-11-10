"""选股主流程：准备 → 粗筛 → 标签 → K 线 → 裁决 → 标注 → 写盘。"""
import datetime as dt
import os
import sys

from .annotate import _annotate_industry, _annotate_turnaround, _write
from .config import CSVDIR, DB, W
from .common.session import MARKET_CLOSE, session_final
from .common.values import fnum
from .datasources.kline import get_kline
from .datasources.market import get_cross, get_labels, get_trading_days
from .output import print_summary
from .params import KLINE_LOOKBACK_DAYS
from .selection import _enrich_market, _select


class CliError(Exception):
    """可直接展示给用户的参数/环境错误。"""


def run(a):
    """执行一次选股；返回进程退出码。"""
    st = {"a": a, "DB": DB}
    _prepare(st)
    _load_cross(st)
    _load_labels(st)
    _load_kline(st)
    _select(st)
    _enrich_market(st)
    _annotate_turnaround(st)
    _annotate_industry(st)
    _write(st)
    print_summary(st)
    return 0


def _prepare(st):
    a = st["a"]
    if not os.path.exists(W):
        raise CliError(f"错误：找不到 westock CLI（{W}）")
    # 校验 --n：负值会让 need = hist[0:]（整段历史），
    # 且 judge 的 age > N 恒真 → 全体被静默剔除，看起来像「今天没有合格股」。
    if a.n < 0:
        raise CliError(
            f"错误：--n 必须 ≥ 0（当前 {a.n}）；每周用 4、每 3 日用 2、每日用 0")
    os.makedirs(DB, exist_ok=True)
    os.makedirs(CSVDIR, exist_ok=True)

    print("[1/6] 交易日历 ...", flush=True)
    now = dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    days = get_trading_days(today, a.refresh)
    if not days:
        raise CliError("错误：交易日历为空")
    S = a.date or days[-1]
    if S not in days:
        prev = max((d for d in days if d <= S), default=None)
        raise CliError(f"错误：基准日 {S} 不是交易日；最近交易日为 {prev or '无'}")
    # 收盘前（含开盘前、盘中）直接拒绝：此时日 K 的收盘价、当日成交量、当日
    # 成交额都还是半成品，而服务端仍会返回一份「当天」的粗筛名单，一旦被缓存
    # 就会在收盘后被当成定稿数据复用，静默产出错名单（详见 common/session.py）。
    if not session_final(S, now):
        prev = max((d for d in days if d < S), default=None)
        raise CliError(
            f"错误：基准日 {S} 尚未收盘定稿"
            f"（当前 {now:%Y-%m-%d %H:%M}，需 {MARKET_CLOSE:%H:%M} 之后）。\n"
            f"      盘中运行时收盘价、量能倍数、换手%都还不完整，"
            f"此时选股得到的名单不可信，因此直接终止。\n"
            f"      请收盘后重跑，或改用 --date {prev or '上一交易日'}。")
    if a.chg_lo > a.chg_hi:
        raise CliError(f"错误：--chg-lo {a.chg_lo} > --chg-hi {a.chg_hi}")
    if not (0 <= a.turn_pct <= 1):
        raise CliError("错误：--turn-pct 必须在 0~1（0=关闭；0.2/0.4/0.6=保留最低比例）")
    if a.turn_min < 0 or a.turn_max < a.turn_min:
        raise CliError(
            f"错误：--turn-min/--turn-max 不合法（{a.turn_min}/{a.turn_max}）")
    hist = [d for d in days if d <= S]
    if len(hist) < a.n + 1:
        raise CliError(f"错误：基准日 {S} 之前交易日不足（需要 {a.n+1} 天）")
    need = hist[-(a.n + 1):]
    span = f"{need[0]}_{need[-1]}"
    print(f"      基准日 {S}，扫描周期 K={a.n+1}，覆盖 {need[0]} ~ {need[-1]}"
          f"（{len(need)} 个交易日）")
    st.update({"days": days, "S": S, "hist": hist, "need": need, "span": span})


def _load_cross(st):
    a, need = st["a"], st["need"]
    print(f"[2/6] 横截面粗筛（{len(need)} 天）...", flush=True)
    cross_by_day = {}
    for d in need:
        rows = get_cross(d, a.chg_lo, a.chg_hi, a.refresh)
        cross_by_day[d] = {r["code"]: r for r in rows if r.get("code")}
        print(f"      {d}: {len(cross_by_day[d])} 只")

    # 字段缺失必须在裁决前报错。服务端只返回表达式引用到的列，
    # 若 CROSS_TPL 改动或旧缓存缺列，judge 的本地复核会全部失败，
    # 看起来像「没有合格股」，实际是数据问题。
    bad_fields = []
    for d in need:
        for c, r in cross_by_day[d].items():
            for k in ("ChangePCT", "MainNetFlow", "MainNetFlow5D"):
                if fnum(r.get(k)) is None:
                    bad_fields.append((d, c, k))
    if bad_fields:
        d, c, k = bad_fields[0]
        raise RuntimeError(
            f"横截面字段缺失 {len(bad_fields)} 处（如 {d} {c} {k}）。"
            f"请检查 CROSS_TPL 是否仍引用这些字段，或加 --refresh 重建缓存。")

    # CHG_HI=20.5% 只覆盖主板 10% 与创业板/科创板 20% 涨停。北交所是 30%
    # 涨跌幅，一旦服务端把 bj 纳入粗筛结果，20.5% 上限会**方向相反地**砍掉
    # 20.5%~30% 的强势启动。当前 cross 缓存里 bj 代码数为 0，先做告警。
    bj_n = sum(1 for d in need for c in cross_by_day[d]
               if str(c).startswith("bj"))
    if bj_n:
        sys.stderr.write(
            f"      ⚠️ 粗筛池出现 {bj_n} 个北交所代码（bj*）：当前 --chg-hi="
            f"{a.chg_hi}% 不覆盖北交所 30% 涨跌幅，请改用 --chg-hi 30.5\n")
    st["cross_by_day"] = cross_by_day


def _load_labels(st):
    a, need = st["a"], st["need"]
    print("[3/6] 排除集（ST + 次新分桶）...", flush=True)
    st_by_day, new_by_day = {}, {}
    for d in need:
        lb = get_labels(d, a.refresh)
        st_by_day[d] = set(lb["st"])
        new_by_day[d] = set(lb["new"])
    print(f"      ST {len(st_by_day[need[-1]])} 只，"
          f"次新 {len(new_by_day[need[-1]])} 只")
    st.update({"st_by_day": st_by_day, "new_by_day": new_by_day})


def _load_kline(st):
    a, need, S = st["a"], st["need"], st["S"]
    cross_by_day = st["cross_by_day"]
    pool = sorted({c for d in need for c in cross_by_day[d]})
    print(f"[4/6] 日K线：候选并集 {len(pool)} 只 ...", flush=True)
    kbeg = (dt.date.fromisoformat(need[0])
            - dt.timedelta(days=KLINE_LOOKBACK_DAYS)).isoformat()
    km, km_missing = get_kline(pool, kbeg, S, a.refresh,
                               require_codes=set(cross_by_day[S]))
    hit_cnt = sum(1 for c in pool if km.get(c))
    print(f"      命中 {hit_cnt}/{len(pool)}")
    if km_missing:
        critical = sorted(set(km_missing) & set(cross_by_day[S]))
        if critical:
            raise RuntimeError(
                f"基准日有成交但完全取不到 K 线：{critical[:8]}"
                f"{'…' if len(critical) > 8 else ''}。"
                f"不能按停牌处理，请加 --refresh 或检查接口。")
        print(f"      ⚠️ {len(km_missing)} 只取不到K线，将按「无K线数据」剔除："
              f"{', '.join(km_missing[:8])}{'…' if len(km_missing) > 8 else ''}")
    st.update({"pool": pool, "pool_n": len(pool), "km": km,
               "km_missing": km_missing})
