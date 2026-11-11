"""候选裁决、行情补全、换手标注与过滤。"""
import sys

from .common.values import cap_to_yuan, fnum
from .datasources.market import get_mv, get_quotes
from .metrics.turnover import apply_turnover
from .params import C2_TOL, C3_RATIO, TURN_WINDOW
from .rules import find_start, judge, reason_cat


def _select(st):
    a, need, S, pool = st["a"], st["need"], st["S"], st["pool"]
    cross_by_day = st["cross_by_day"]
    km, st_by_day, new_by_day = st["km"], st["st_by_day"], st["new_by_day"]
    print("[5/6] 逐条裁决 ...", flush=True)
    hit = []
    rej_raw = {}     # 原文原因 -> 次数（保留真实 N/age/量能比/日期）
    rej_code = {}    # code -> [阶段码, 原文原因]，逐股可追溯
    rej = {}         # 顶层统计：reason_cat(原文) -> 次数（仅供展示与跨批对比）
    for c in pool:
        name = next((cross_by_day[d][c].get("name") for d in need
                     if c in cross_by_day[d]), "")
        det, why0 = find_start(c, name, km.get(c, []), S)
        if det is None:
            rej_raw[why0] = rej_raw.get(why0, 0) + 1
            rej_code[c] = ["FS", why0]
            continue
        ok, why = judge(det, km[c], S, a.n, cross_by_day, st_by_day, new_by_day,
                        a.k_vol, a.ma60_gap, C2_TOL, C3_RATIO,
                        a.chg_lo, a.chg_hi)
        if ok:
            hit.append(det)
        else:
            rej_raw[why] = rej_raw.get(why, 0) + 1
            rej_code[c] = ["JD", why]
    st.update({"hit": hit, "rej_raw": rej_raw, "rej_code": rej_code, "rej": rej})


def _enrich_market(st):
    a, S, hist = st["a"], st["S"], st["hist"]
    cross_by_day, km = st["cross_by_day"], st["km"]
    hit = st["hit"]
    print("[6/6] 取行情 / 计算换手与市值比 / 写结果 ...", flush=True)
    q = get_quotes([x["code"] for x in hit], S, a.refresh) if hit else {}
    # ⚠️ 行情快照缺失会让收盘价/换手等展示列空白；绝不能因此静默剔除。
    if hit:
        missing = [x["code"] for x in hit
                   if not cap_to_yuan(fnum((q.get(x["code"]) or {})
                                           .get("circulating_market_cap")))]
        if missing:
            raise RuntimeError(
                f"行情数据缺失 {len(missing)}/{len(hit)} 只（如 {missing[:5]}）。"
                f"展示列（收盘价/换手）无法填充，终止以免产出不可信名单。"
                f"可重跑一次或加 --refresh。")
    # ⚠️ 同上一道防线，但针对「启动日净流入」字段：字段缺失时市值比同样算不出来。
    #    典型成因是 CROSS_TPL 不再引用 MainNetFlow —— 服务端只返回被引用的列，
    #    于是一切 nf=None、ratio=None，展示列静默变空。v1.3 起净流入不再参与裁决，
    #    所以更要在写盘前报错，否则只会得到一份「市值比全空」却看不出异常的名单。
    if hit:
        noflow = [x["code"] for x in hit
                  if fnum(((cross_by_day.get(x["d_star"]) or {})
                           .get(x["code"]) or {}).get("MainNetFlow")) is None]
        if noflow:
            raise RuntimeError(
                f"启动日净流入字段缺失 {len(noflow)}/{len(hit)} 只（如 {noflow[:5]}）。"
                f"市值比无法计算，终止以免产出不可信名单。"
                f"请检查 CROSS_TPL 是否仍引用 MainNetFlow"
                f"（服务端只返回表达式引用到的列）。")

    # 市值比 v1.1 起**只作展示、不参与裁决**（审核结论：该条件筛出的是高换手热门票，
    # 相对粗筛池 20 日超额为负）。分母仍用 d* 日流通市值，与分子 MainNetFlow@d* 同日。
    # mv_<d>.json 与 download_history 共用。
    mv_by_day = {}
    turn_meta = {"启用": False, "原因": "无候选"}
    final, rej2 = [], {}
    if hit:
        d_star_days = sorted({x["d_star"] for x in hit})
        print(f"      市值比口径：分子/分母同为 d* 日（{len(d_star_days)} 个日期："
              f"{', '.join(d_star_days)}）", flush=True)
        for d in d_star_days:
            mv_by_day[d] = get_mv(d, a.refresh)
        lack = [f"{x['code']}@{x['d_star']}" for x in hit
                if not ((mv_by_day.get(x["d_star"]) or {})
                        .get(x["code"]) or [None])[0]]
        if lack:
            raise RuntimeError(
                f"启动日流通市值缺失 {len(lack)}/{len(hit)} 只（如 {lack[:5]}）。"
                f"市值/换手展示列无法填充，终止以免产出不可信名单"
                f"（可重跑或加 --refresh）。")
        # 换手标注/过滤：分母用 d* 里最近一天（与 S 最多差 a.n 个交易日），
        # 这样不必为 S 日再依赖一次网络请求；20 日均值对几天的市值漂移不敏感。
        # 默认只排序标注（保持「上穿+放量」的原意）；--turn-pct 0.4 才是硬过滤。
        kept, turn_meta, dropped = apply_turnover(
            hit, cross_by_day[S], km, mv_by_day[d_star_days[-1]],
            hist[-TURN_WINDOW:], a.turn_pct, a.turn_min, a.turn_max)
        if turn_meta.get("启用"):
            for code, why in dropped:
                rej2[why] = rej2.get(why, 0) + 1
                rej_code = st["rej_code"]
                rej_code[code] = ["TN", why]
            hit = kept
            print(f"      换手过滤：{turn_meta['保留档位']}，剔除线 "
                  f"{turn_meta['剔除线%']}%（池内 {turn_meta['池内样本']} 只），"
                  f"剔除 {len(dropped)} 只、保留 {len(kept)} 只；"
                  f"{turn_meta['自适应']}", flush=True)
            if len(kept) < a.turn_min:
                sys.stderr.write(
                    f"      ⚠️ 换手过滤后仅 {len(kept)} 只（<{a.turn_min}）："
                    f"可加 --turn-pct 0.6 再放宽，或接受本期名单偏少\n")
        else:
            hit = kept
            print(f"      换手标注：池内 {turn_meta.get('池内样本', 0)} 只，"
                  f"{turn_meta['原因']}；名单按换手分位升序排列", flush=True)
    for x in hit:
        r = q.get(x["code"], {})

        def num(k):
            return fnum(r.get(k))
        nf = fnum(cross_by_day[x["d_star"]][x["code"]].get("MainNetFlow"))
        nf5 = fnum(cross_by_day[x["d_star"]][x["code"]].get("MainNetFlow5D"))
        chg = fnum(cross_by_day[x["d_star"]][x["code"]].get("ChangePCT"))
        # 分母 = d* 日流通市值（元）；mv 文件里已经是元
        pair = mv_by_day[x["d_star"]][x["code"]]
        fc = float(pair[0])
        tc = float(pair[1]) if pair[1] else None
        ratio = nf / fc * 100 if (nf is not None and fc) else None
        x.update({
            "chg": chg, "net_flow": nf, "net_flow_5d": nf5,
            "flow_ratio": round(ratio, 3) if ratio is not None else None,
            "flow_cap_day": x["d_star"],
            "r20": round(x["close"] / x["ma20"] * 100 - 100, 2),
            "r60": round(x["close"] / x["ma60"] * 100 - 100, 2),
            "m20_m60": round(x["ma20"] / x["ma60"], 4),
            "float_cap_yi": round(fc / 1e8, 2) if fc else None,
            "total_cap_yi": round(tc / 1e8, 2) if tc is not None else None,
            "price": num("price"),
        })
        final.append(x)
    for k, v in rej2.items():
        st["rej_raw"][k] = st["rej_raw"].get(k, 0) + v

    # 顶层统计由「原文 -> 次数」聚合得到，原文与逐股映射另行落盘。
    for why, n in st["rej_raw"].items():
        k = reason_cat(why)
        st["rej"][k] = st["rej"].get(k, 0) + n

    # 计数断言：pool 里每只股票有且只有一种去向（入选 / 某一阶段被剔）。
    # 放在 rej2 合并之后，否则会漏算「换手分位」那几只。
    n_rej = sum(st["rej_raw"].values())
    final_codes = {x["code"] for x in final}
    pool = st["pool"]
    if len(pool) != len(final) + n_rej:
        raise RuntimeError(
            f"计数不自洽：粗筛池 {len(pool)} ≠ 入选 {len(final)} + 剔除 {n_rej}")
    if set(st["rej_code"]) & final_codes:
        raise RuntimeError(
            f"同一只股票既入选又被剔除：{sorted(set(st['rej_code']) & final_codes)[:5]}")
    if set(st["rej_code"]) | final_codes != set(pool):
        miss = sorted(set(pool) - (set(st["rej_code"]) | final_codes))
        extra = sorted((set(st["rej_code"]) | final_codes) - set(pool))
        raise RuntimeError(
            f"逐股去向不完整：池中无去向 {miss[:5]}；池外多出 {extra[:5]}")
    st.update({"q": q, "mv_by_day": mv_by_day, "turn_meta": turn_meta,
               "final": final})
