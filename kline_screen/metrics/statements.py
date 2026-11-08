"""财报行选择器：最新期、年报、订单代理。"""
from ..common.values import end_date, fnum, pub_date


def _order_proxy(r):
    """订单代理 = 合同负债 + 预收款项；两者都没有返回 None。

    ⚠️ 必须用 AdvanceReceipts（预收款项），不能用 AdvancePayment（预付款项）。
    后者是公司付给供应商的资产，与客户预收方向相反。
    """
    cl = fnum(r.get("ContractLiability"))
    ar = fnum(r.get("AdvanceReceipts"))
    if cl is None and ar is None:
        return None
    return (cl or 0.0) + (ar or 0.0)


def _stmt_latest(rows, day):
    """基准日前最新一期报表；无则 None。"""
    valid = [r for r in rows
             if pub_date(r.get("InfoPublDate")) and end_date(r)
             and pub_date(r.get("InfoPublDate")) <= day]
    return max(valid, key=end_date) if valid else None


def _stmt_annual(rows, day, n=3):
    """基准日前最近 n 个年报（12-31），按报告期升序。"""
    valid = [r for r in rows
             if pub_date(r.get("InfoPublDate")) and end_date(r)
             and pub_date(r.get("InfoPublDate")) <= day
             and end_date(r).endswith("-12-31")]
    valid.sort(key=end_date)
    return valid[-n:]
