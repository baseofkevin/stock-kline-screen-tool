"""入选后的业绩转变、行业标注与写盘。"""
import os
import sys

from .config import CSVDIR, DB
from .common.storage import count_csv_rows, jdump, write_csv_atomic
from .datasources.industry import get_industry_map
from .metrics.turnaround import compute_turnaround
from .output import build_result, csv_row
from .params import CSV_HEADER


def _annotate_turnaround(st):
    a, S, final = st["a"], st["S"], st["final"]
    # ---- 业绩转变标注（只标注，不影响上面任何裁决结果）----
    turn, turn_failed = compute_turnaround(
        [x["code"] for x in final], S, a.refresh)
    if turn is None:
        print("      ⚠️ 财务数据获取失败：「毛利率改善」列留空（选股结果不受影响）",
              file=sys.stderr)
    elif turn_failed:
        print(f"      ⚠️ {len(turn_failed)} 只财务数据缺失，标「无数据」："
              f"{', '.join(turn_failed[:8])}{'…' if len(turn_failed) > 8 else ''}",
              file=sys.stderr)
    for x in final:
        if turn is None:
            x.update({"turn_p1": "", "turn_detail": None, "turn_gm": ""})
            continue
        m = turn.get(x["code"])
        if m is None:
            x.update({"turn_p1": "无数据", "turn_detail": None, "turn_gm": ""})
            continue
        x["turn_p1"] = m["p1_label"]
        x["turn_detail"] = m
        x["turn_gm"] = m.get("gm_desc") or ""
    if turn is not None and final:
        order = ["是", "否", "无数据", "不适用"]
        cnt = {}
        for x in final:
            v = x.get("turn_p1") or "无数据"
            cnt[v] = cnt.get(v, 0) + 1
        print("      ①毛利率环比改善：" + " ｜ ".join(
            f"{k} {cnt[k]} 只" for k in order if cnt.get(k)))
    st.update({"turn": turn, "turn_failed": turn_failed})


def _annotate_industry(st):
    a, final = st["a"], st["final"]
    # ---- 行业归属（申万一级/二级）：只展示，不参与裁决 ----
    # 放在最后一步：只为最终名单打标签，未入选的股票不查；缓存命中时零请求。
    if final:
        ind = get_industry_map(refresh=a.refresh or a.refresh_industry)
        for x in final:
            x["sw1"] = ind["sw1"].get(x["code"]) or "未分类"
            x["sw2"] = ind["sw2"].get(x["code"]) or "未分类"
        unknown = [x["code"] for x in final if x["sw2"] == "未分类"]
        if unknown:
            sys.stderr.write(
                f"      ⚠️ {len(unknown)} 只无申万二级归属"
                f"（{', '.join(unknown[:5])}{'…' if len(unknown) > 5 else ''}），"
                f"CSV 记「未分类」；可加 --refresh-industry 重建行业表\n")


def _write(st):
    S, final = st["S"], st["final"]
    # 展示顺序：池内换手分位升序（最冷在前，方便从低关注度一端开始审核）；
    # 分位为 None（数据不足）的排最后。只影响阅读顺序，不改名单构成。
    # ⚠️ 这里是**原地排序**（不是 sorted）：CSV 行与 result JSON 的「名单」共用
    #    同一个 list，靠原地排序保证两份产物顺序一致。改成 sorted() 会让
    #    CSV 与 JSON 顺序分叉。
    final.sort(key=lambda x: (x.get("turn20_rank") is None,
                              x.get("turn20_rank") or 0.0, x["code"]))

    # ---- 写 CSV（原子写 + 行数校验）----
    csv_path = os.path.join(CSVDIR, f"选股_{S}.csv")
    res_path = os.path.join(DB, f"result_{S}.json")
    csv_rows = [csv_row(x) for x in final]
    write_csv_atomic(csv_path, CSV_HEADER, csv_rows)
    n_csv = count_csv_rows(csv_path)
    if n_csv != len(final):
        raise RuntimeError(
            f"CSV 行数 {n_csv} ≠ 名单 {len(final)}：写盘后立即校验失败，"
            f"不写 result JSON（{csv_path}）")

    # ---- 写运行明细（便于追溯）----
    st.update({"csv_path": csv_path, "res_path": res_path, "n_csv": n_csv})
    result = build_result(st)
    jdump(res_path, result)

    # 收尾成对校验。CSV 先写、JSON 后写，正常结束必须两者都在且行数一致；
    # 任何一半缺失都说明产物被外部改动或写盘异常，不能静默返回 0。
    if not os.path.exists(csv_path) or not os.path.exists(res_path):
        raise RuntimeError(
            f"产物缺失：CSV {'有' if os.path.exists(csv_path) else '无'} / "
            f"JSON {'有' if os.path.exists(res_path) else '无'}")
    if count_csv_rows(csv_path) != len(final):
        raise RuntimeError(
            f"产物行数不一致：CSV {count_csv_rows(csv_path)} ≠ 名单 {len(final)}")
