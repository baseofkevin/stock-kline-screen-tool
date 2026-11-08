"""PS 估值列（只展示）。"""
from ..common.values import fnum
from ..params import FIN_TYPES


def valuation_metrics(ratio_rows, bal_rows, quote):
    """PS（市销率）估算，**只作展示**，不筛选、不排序、不改名单。

    返回 {"ps": float|None}：
      · ps = 总市值 ÷ 最新报告期营业总收入TTM；数据不足时留空。
    金融股（银行/保险/证券）的营收口径与一般企业不同，PS 标不适用。
    PS 只用**最新报告期**的营业总收入TTM：早年的 TTM 与当前市值不是同一时点，
    混用会算出「市值 ÷ 三年前营收」这种没有意义的数。
    v1.8 起删除 DCF 计算与三列 DCF 输出。
    """
    quote = quote or {}
    ent = next((r.get("EnterpriseType")
                for r in list(ratio_rows or []) + list(bal_rows or [])
                if r.get("EnterpriseType")), "") or ""
    fin = any(k in ent for k in FIN_TYPES)
    if fin:
        return {"ps": None}

    latest = max((r for r in (ratio_rows or []) if r.get("EndDate")),
                 key=lambda r: str(r.get("EndDate")), default=None)
    cap = fnum(quote.get("total_market_cap"))
    rev_ttm = fnum((latest or {}).get("OperatingRevenueTTM"))
    if cap and rev_ttm and rev_ttm > 0:
        return {"ps": round(cap / rev_ttm, 2)}
    return {"ps": None}
