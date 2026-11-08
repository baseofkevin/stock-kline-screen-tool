"""财务三表取数与限频重试。"""
import os
import sys
import time

from ..config import DB
from ..common.storage import jdump, jload
from ..common.westock import load_westock_module, retry
from ..params import (FIN_BATCH, FIN_PERIODS, TURN_FIN_BACKOFF,
                      TURN_FIN_BATCH, TURN_FIN_PERIODS, TURN_FIN_RETRIES,
                      TURN_FIN_SLEEP)


def get_finance(codes, day, refresh):
    """拉取候选股的利润表核心字段，按基准日缓存。

    供「业绩转变」标注的营收/成本差分兜底使用。
    返回 ({code: [rows]}, 失败代码列表)。rows 是 data_finance 的原始报告期记录，
    含 OperatingRevenue / OperatingCost / EndDate / InfoPublDate。
    """
    path = os.path.join(DB, f"finance_{day}.json")
    cached = {} if refresh else jload(path, {})
    todo = [c for c in codes if c not in cached]
    failed = []
    if not todo:
        return cached, failed
    try:
        wk = load_westock_module()
    except Exception as e:
        sys.stderr.write(f"  [失败] 加载 westock 模块：{e}\n")
        return cached, list(codes)
    for i in range(0, len(todo), FIN_BATCH):
        ch = todo[i:i + FIN_BATCH]
        d = retry(lambda: wk.call("data_finance", {
            "codes": ",".join(ch), "fields": "core",
            "num": FIN_PERIODS, "type": "income"}),
            what=f"财务数据 第{i // FIN_BATCH + 1}批")
        if d is None:
            failed.extend(ch)
            continue
        data = d.get("data") if isinstance(d, dict) else None
        if not isinstance(data, dict):
            failed.extend(ch)
            continue
        for c in ch:
            rows = data.get(c)
            if rows:
                cached[c] = rows
            else:
                failed.append(c)
        jdump(path, cached)
        time.sleep(0.4)
    return cached, failed


def fin_retry(fn, tries=TURN_FIN_RETRIES, what=""):
    """财务接口专用重试：服务端「服务限频」需要长退避。

    通用 retry() 的 1.5s 节奏对 data_finance 不够；实测限频后要等 10s 以上
    才恢复，否则越试越糟。返回 None 表示彻底失败。
    任何失败都按指数退避，且下限不低于 TURN_FIN_BACKOFF（默认 15s），
    不依赖服务端错误文案。
    """
    last = None
    for i in range(tries):
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as e:
            last = e
            if i < tries - 1:
                msg = str(e)
                wait = max(TURN_FIN_BACKOFF, min(60.0, 5.0 * (2 ** i)))
                sys.stderr.write(
                    f"\n  [重试] {what}：{msg[:70]}，{wait:.0f}s 后重试\n")
                time.sleep(wait)
    sys.stderr.write(f"\n  [失败] {what}（重试 {tries} 次）：{last}\n")
    return None


def get_finance_stmt(codes, day, refresh, typ, fields, cache_name):
    """拉取某类财务报表（balance / balancesheet）并按基准日缓存。

    与 get_finance() 的差异：
      · 单批更小、批间更慢，专门应对 data_finance 的「服务限频」；
      · 批内缺失的代码会逐只补取一次；
      · 只缓存有数据的股票，失败留下的空位下次运行会自动重试。
    返回 ({code: [rows]}, 失败代码列表)。
    """
    path = os.path.join(DB, cache_name.format(day=day))
    cached = {} if refresh else jload(path, {})
    todo = [c for c in codes if c not in cached]
    failed = []
    if not todo:
        return cached, failed
    try:
        wk = load_westock_module()
    except Exception as e:
        sys.stderr.write(f"  [失败] 加载 westock 模块：{e}\n")
        return cached, list(codes)

    base = {"fields": fields, "num": TURN_FIN_PERIODS, "type": typ}
    n_batch = (len(todo) + TURN_FIN_BATCH - 1) // TURN_FIN_BATCH
    for i in range(0, len(todo), TURN_FIN_BATCH):
        ch = todo[i:i + TURN_FIN_BATCH]
        d = fin_retry(lambda ch=ch: wk.call(
            "data_finance", dict(base, codes=",".join(ch))),
            what=f"{typ} 第{i // TURN_FIN_BATCH + 1}/{n_batch} 批")
        data = d.get("data") if isinstance(d, dict) else None
        if isinstance(data, dict):
            for c in ch:
                rows = data.get(c)
                if rows:
                    cached[c] = rows
        # 批内缺口逐只补取：服务端偶发只返回部分代码，不能当成「无数据」
        missing = [c for c in ch if c not in cached]
        for c in missing:
            d1 = fin_retry(lambda c=c: wk.call(
                "data_finance", dict(base, code=c)), what=f"{typ} 补取 {c}")
            rows = ((d1 or {}).get("data") or {}).get(c) \
                if isinstance(d1, dict) else None
            if rows:
                cached[c] = rows
            else:
                failed.append(c)
            time.sleep(TURN_FIN_SLEEP)
        jdump(path, cached)
        sys.stderr.write(
            f"\r  {typ} {i // TURN_FIN_BATCH + 1}/{n_batch} 批"
            f"  库内 {len(cached)} 只")
        sys.stderr.flush()
        time.sleep(TURN_FIN_SLEEP)
    if todo:
        sys.stderr.write("\n")
    return cached, failed
