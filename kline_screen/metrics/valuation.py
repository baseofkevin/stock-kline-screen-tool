"""PS / DCF 估值列（只展示）。"""
from ..common.values import fnum
from ..params import (DCF_G, DCF_G1_CAP, DCF_MIN_ANNUAL, DCF_N, DCF_R,
                      FIN_TYPES)
from .statements import annual_rows


def valuation_metrics(ratio_rows, bal_rows, quote):
    """PS 与 DCF 简单估值（**只作展示**，不筛选、不排序、不改名单）。

    返回 {"ps": float|None, "fair": float|None, "margin_pct": float|None,
          "note": str}：
      · fair       = DCF 合理价（元）
      · margin_pct = (合理价 − 现价) / 合理价 × 100，正数=低估
      · note       = 不适用原因（适用时为空串）
    DCF 口径见文件头的 DCF_* 常量；理由不充分时只给 note，不强行给数。

    金融股（银行/保险/证券）的营收与现金流口径与一般企业不同，PS 与 DCF 都标不适用。
    PS 只用**最新报告期**的营业总收入TTM：早年的 TTM 与当前市值不是同一时点，
    混用会算出「市值 ÷ 三年前营收」这种没有意义的数。
    """
    quote = quote or {}
    ent = next((r.get("EnterpriseType")
                for r in list(ratio_rows or []) + list(bal_rows or [])
                if r.get("EnterpriseType")), "") or ""
    fin = any(k in ent for k in FIN_TYPES)
    out = {"ps": None, "fair": None, "margin_pct": None, "note": ""}

    if not fin:
        latest = max((r for r in (ratio_rows or []) if r.get("EndDate")),
                     key=lambda r: str(r.get("EndDate")), default=None)
        cap = fnum(quote.get("total_market_cap"))
        rev_ttm = fnum((latest or {}).get("OperatingRevenueTTM"))
        if cap and rev_ttm and rev_ttm > 0:
            out["ps"] = round(cap / rev_ttm, 2)

    if fin:
        out["note"] = "金融股不适用"
        return out

    ann = annual_rows(ratio_rows)
    if len(ann) < DCF_MIN_ANNUAL:
        out["note"] = f"年报不足{DCF_MIN_ANNUAL}年"
        return out
    fcf = next((fnum(r.get("FCFF")) for r in reversed(ann)
                if fnum(r.get("FCFF")) is not None), None)
    if fcf is None:
        out["note"] = "无自由现金流数据"
        return out
    if fcf <= 0:
        out["note"] = "自由现金流≤0"
        return out
    rev = [fnum(r.get("OperatingRevenue")) for r in ann]
    if any(v is None or v <= 0 for v in rev):
        out["note"] = "营收数据不全"
        return out
    g1 = (rev[-1] / rev[0]) ** (1 / (len(rev) - 1)) - 1
    g1 = max(0.0, min(DCF_G1_CAP, g1))
    r_ = DCF_R
    ev = sum(fcf * (1 + g1) ** t / (1 + r_) ** t for t in range(1, DCF_N + 1))
    ev += fcf * (1 + g1) ** DCF_N * (1 + DCF_G) / (r_ - DCF_G) / (1 + r_) ** DCF_N
    shares = fnum(quote.get("total_shares"))
    price = fnum(quote.get("price"))
    if not shares or shares <= 0 or not price:
        out["note"] = "缺股本或现价"
        return out
    fair = ev / shares
    out["fair"] = round(fair, 2)
    out["margin_pct"] = round((fair - price) / fair * 100, 1)
    return out
