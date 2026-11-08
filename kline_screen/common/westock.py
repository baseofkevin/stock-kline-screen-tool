"""westock CLI 调用、markdown 解析与重试。"""
import subprocess
import sys
import time

from ..config import W


def run(args):
    p = subprocess.run(args, capture_output=True, timeout=900)
    return p.stdout.decode("utf-8", "replace")


def md(txt):
    """解析 westock CLI 的 markdown 表格"""
    tbl = [l for l in txt.splitlines() if l.strip().startswith("|")]
    if len(tbl) < 2:
        return []
    hdr = [x.strip() for x in tbl[0].strip().strip("|").split("|")]
    out = []
    for ln in tbl[1:]:
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) == len(hdr) and not all(set(v) <= set("-: ") for v in c):
            out.append(dict(zip(hdr, c)))
    return out


def retry(fn, tries=4, base=1.5, what="", allow_empty=False):
    """返回 None 表示彻底失败（调用方必须处理，不得静默继续）。

    allow_empty=True 时，空列表/空字符串视为**成功但为空**；只应在
    「空结果本身合法」的场景使用（如标签分页的下一页）。默认 False，
    因为历史日期横截面接口会把失败静默表现为空列表。
    """
    for i in range(tries):
        try:
            r = fn()
            if r or (allow_empty and r is not None):
                return r
        except Exception:
            pass
        if i < tries - 1:
            time.sleep(base * (i + 1))
    sys.stderr.write(f"  [失败] {what}（重试 {tries} 次）\n")
    return None


def load_westock_module():
    """把 westock CLI 当模块加载，复用它已有的鉴权 / JSON-RPC。

    westock CLI 目前没有 finance 子命令，而 data_finance 是拿财务历史的唯一入口。
    这里不复制令牌与续期逻辑，避免两套认证代码各自过期。
    """
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("westock_cli", W)
    spec = importlib.util.spec_from_loader("westock_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    if not hasattr(mod, "call"):
        raise RuntimeError("当前 westock CLI 不支持 data_finance 的模块调用")
    return mod
