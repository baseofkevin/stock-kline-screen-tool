"""申万一级/二级行业归属（只展示）。"""
import os
import sys
import time

from ..config import DB
from ..common.storage import jdump, jload
from ..common.values import fnum
from ..common.westock import load_westock_module, retry

INDUSTRY_MAP = os.path.join(DB, "industry_map.json")
INDUSTRY_SCHEMA = 1
INDUSTRY_TTL_DAYS = 30
INDUSTRY_SLEEP = 0.15
INDUSTRY_MAX_FAIL = 6
INDUSTRY_SCOPES = (("sw1", "industry_list_sw1", "申万一级"),
                   ("sw2", "industry_list_sw2", "申万二级"))


def _sector_items(wk, scope):
    """申万行业清单 [{code, name, sectorCode}, ...]；失败返回 []。"""
    d = retry(lambda: wk.call("data_sector", {"mode": "list", "scope": scope}),
              tries=3, what=f"申万行业清单 {scope}")
    if not isinstance(d, dict):
        return []
    items = next((v for k, v in d.items() if k.startswith("industry_list")), None)
    if isinstance(items, dict):
        items = items.get("items")
    return [it for it in (items or [])
            if isinstance(it, dict) and (it.get("sectorCode") or it.get("code"))]


def _sector_members(wk, code, name):
    """某行业成分股；请求失败返回 None（与「行业本身为空」区分开）。"""
    d = retry(lambda: wk.call("data_sector", {
        "mode": "constituent", "code": code, "limit": 0}),
        tries=3, what=f"行业成分股 {name}")
    if not isinstance(d, dict) or not d:
        return None
    blk = d.get(next(iter(d)))
    if not isinstance(blk, dict):
        return None
    stocks = blk.get("stocks") or []
    total = fnum(blk.get("totalStocks"))
    if total and len(stocks) < total:
        sys.stderr.write(f"      ⚠️ 行业 {name} 成分股只返回 {len(stocks)}/{int(total)}\n")
    return stocks


def build_industry_map():
    """重建 code → 申万一级/二级名称表；失败行业过多返回 None（不覆盖旧缓存）。"""
    wk = load_westock_module()
    out = {"schema": INDUSTRY_SCHEMA, "ts": int(time.time()),
           "asof": time.strftime("%Y-%m-%d"),
           "来源": "westock data_sector（申万一级 31 / 申万二级 124）",
           "sw1": {}, "sw2": {}}
    for key, scope, label in INDUSTRY_SCOPES:
        items = _sector_items(wk, scope)
        if not items:
            sys.stderr.write(f"      ⚠️ {label}行业清单为空，放弃重建\n")
            return None
        got, failed = 0, []
        for it in items:
            code = it.get("sectorCode") or f"{key}_{it['code']}"
            name = it.get("name") or it["code"]
            stocks = _sector_members(wk, code, name)
            if stocks is None:
                failed.append(name)
                if len(failed) >= INDUSTRY_MAX_FAIL:
                    sys.stderr.write(
                        f"      ⚠️ {label}行业累计失败 {len(failed)} 个，放弃重建\n")
                    return None
                continue
            for r in stocks:
                c = r.get("code")
                if c:
                    out[key][c] = name
            got += 1
            time.sleep(INDUSTRY_SLEEP)
        print(f"      {label}行业 {got}/{len(items)} 个，覆盖 {len(out[key])} 只",
              flush=True)
        if failed:
            sys.stderr.write(f"      ⚠️ {label}行业失败 {len(failed)} 个：{failed[:5]}\n")
    return out if out["sw1"] else None


def get_industry_map(refresh=False, builder=None):
    """读取/重建行业归属缓存；返回 {"sw1": {code: 名}, "sw2": {...}, ...}。

    优先级：新鲜缓存 →（过期或强制）重建 → 过期旧缓存 → 空表。
    绝不抛异常：行业只是展示列，不能因为它让整轮选股失败。
    """
    builder = builder or build_industry_map
    cached = jload(INDUSTRY_MAP, None)
    usable = (isinstance(cached, dict) and cached.get("schema") == INDUSTRY_SCHEMA
              and isinstance(cached.get("sw1"), dict) and cached["sw1"])
    if usable and not refresh:
        age = time.time() - (fnum(cached.get("ts") or 0) or 0)
        if age < INDUSTRY_TTL_DAYS * 86400:
            return cached
    sys.stderr.write(
        f"      行业归属缓存{'已过期' if usable else '不存在'}，"
        f"重建申万行业表（约 2~3 分钟）...\n")
    fresh = None
    try:
        fresh = builder()
    except Exception as e:      # noqa: BLE001 —— 展示列不允许中断主流程
        sys.stderr.write(f"      ⚠️ 行业表重建失败：{e}\n")
    if fresh:
        try:
            jdump(INDUSTRY_MAP, fresh)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"      ⚠️ 行业表落盘失败：{e}\n")
        return fresh
    if usable:
        sys.stderr.write("      ⚠️ 沿用过期行业缓存（只影响展示列）\n")
        return cached
    sys.stderr.write("      ⚠️ 无可用行业数据：CSV 行业列记「未分类」\n")
    return {"sw1": {}, "sw2": {}}
