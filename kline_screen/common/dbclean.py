"""database/ 的定期清理：只保留「下次运行还用得上」的文件。

为什么要这个模块
----------------
database/ 里所有文件都是**中间缓存**，没有一份是最终产物（最终产物是
csv_output/选股_<S>.csv）。早期设计只删 kline 底稿且要手动加 --prune-kline，
结果就是目录只增不减：每周新增约 66 MB，一年 3.4 GB；实测 2026-11-12 时
340 个文件里 296 个是一周前的死数据。

清理原则
--------
按「下次运行（下一个基准日 S'）还会不会读它」分类，而不是按「谁最旧」：

1. **kline_*.json 底稿**：只留最新 1 份（KLINE_KEEP，默认 2，留一份给回滚）。
   新底稿是旧底稿的**超集**（代码见 kline.get_kline 的注释：保留底稿里的全部
   代码 + 区间向后延展），所以删旧的不会丢股票。单份 65 MB，是目录里的大头。

2. **按日期命名的逐日缓存**（cross / labels / mv / quote / result / finance*）：
   只留最近 KEEP_DAYS 天（默认 14，约两期扫描）。
   下一次运行的 need 只有最近 5 个交易日，K 线取数不需要这些文件；
   保 14 天是为了「重跑上一期」和「临时回看一期」不用重下。

3. **不带日期的常驻文件**（trading_calendar / industry_map / gene_industry_map /
   history_meta）：**永不删**，它们会被覆盖更新，体积固定。

4. **gene_snapshot_* / 其它认不出来的文件**：只留最近 KEEP_DAYS 天，
   但绝不删除 mtime 在 protect 之内的（本次运行刚写的）。

安全约束
--------
- 永远不删本次运行正在使用的文件（protect 参数 / mtime 判断双重保险）。
- 只在「名单已经落盘并校验通过」之后调用（pipeline._write 之后），
  取数失败抛异常时不会走到这里，旧数据一定还在。
- 删除失败（权限、文件被占用）只跳过，绝不中断运行。
- 所有动作打印出来，让人看得见删了什么、省了多少空间。
- `--no-clean` 可完全关闭（排查问题时想保住现场）。

怎么改最少
----------
只有两处改动：
  1. 新增本文件；
  2. pipeline.run() 在 _write(st) 之后调一次 clean(st)。
其余代码一行不动 —— 因为它只做**删文件**，不改变任何取数/选股逻辑。
"""
import os
import re
import sys

from ..config import DB
from ..params import KLINE_KEEP

# 按日期命名的逐日 / 逐期缓存：<前缀>_<YYYY-MM-DD>[.<后缀>].json
#   cross_2026-11-10.json / cross_2026-11-10.meta.json / labels_2026-11-10.json
#   mv / quote / result / finance / finance_bal / finance_ratio / gene_snapshot
_DAY_RE = re.compile(r"^(?P<pre>[a-z_]+)_(?P<day>\d{4}-\d{2}-\d{2})(?P<tail>\..*)?$")
# kline 底稿：kline_<起>_<止>.json / .meta.json
_KLINE_RE = re.compile(r"^kline_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})(\..*)?$")

KEEP_DAYS = 14          # 逐日缓存保留天数（约两期扫描）
# 不按日期清理的常驻文件：被覆盖更新、体积固定
_PINNED = {
    "trading_calendar.json", "industry_map.json",
    "gene_industry_map.json", "history_meta.json",
}


def _is_pinned(name):
    return name in _PINNED


def _day_of(name):
    """文件名里的日期；认不出返回 None。"""
    m = _DAY_RE.match(name)
    if m:
        return m.group("day")
    m = _KLINE_RE.match(name)
    if m:
        return m.group(2)          # kline 用「止」判断新旧
    return None


def _kline_end(name):
    m = _KLINE_RE.match(name)
    return m.group(2) if m else ""


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def collect(keep_days=KEEP_DAYS, keep_kline=KLINE_KEEP, protect=(),
            protect_mtime_within=0):
    """算出该删哪些文件（不删），返回 (to_delete, kept_bytes, drop_bytes)。

    protect：文件名集合，永不删（本次运行刚写的，如当前 kline 底稿）。
    protect_mtime_within：mtime 在这么多秒以内的文件也不删（默认 0 = 不启用）。
        用于「不想传具体路径、但必须保住刚写文件」的场景。
    """
    if not os.path.isdir(DB):
        return [], 0, 0
    protected = {os.path.basename(p) for p in protect if p}
    names = [n for n in os.listdir(DB) if n.endswith(".json")]
    if not names:
        return [], 0, 0
    if protect_mtime_within:
        import time as _t
        now = _t.time()
        for n in names:
            try:
                if now - os.path.getmtime(os.path.join(DB, n)) <= protect_mtime_within:
                    protected.add(n)
            except OSError:
                protected.add(n)        # 读不到 mtime 就不动它

    # ---- kline 底稿：按「止」倒序，留最新 keep_kline 份（连同 .meta.json）----
    # ⚠️ .meta.json 不能单独参与排序：它会被 _KLINE_RE 匹配成另一份底稿，
    #    导致「保留第 N 份」时挑到的是某份的 meta，真身反而被删。
    #    所以先只对主文件 .json 排序，再成对展开。
    kl_main = [n for n in names
               if _KLINE_RE.match(n) and not n.endswith(".meta.json")]
    kl_main.sort(key=_kline_end, reverse=True)
    keep_kl_names = set()
    for n in kl_main[:keep_kline]:
        keep_kl_names.add(n)
        keep_kl_names.add(n[:-5] + ".meta.json")
    for p in protected:                 # 本次运行的底稿：主文件与 meta 都保住
        if _KLINE_RE.match(p):
            keep_kl_names.add(p)
            if p.endswith(".json") and not p.endswith(".meta.json"):
                keep_kl_names.add(p[:-5] + ".meta.json")

    # ---- 逐日缓存：日期 < 阈值则删 ----
    days = sorted({d for n in names if (d := _day_of(n)) and not _KLINE_RE.match(n)})
    cutoff = days[-keep_days] if len(days) > keep_days else None

    to_delete, drop_bytes = [], 0
    for n in names:
        if _is_pinned(n) or n in protected:
            continue
        if _KLINE_RE.match(n):
            # 主文件与 .meta.json 都按配对后的保留名单判断
            if n not in keep_kl_names:
                to_delete.append(n)
                drop_bytes += _size(os.path.join(DB, n))
            continue
        d = _day_of(n)
        if d and cutoff and d < cutoff:
            to_delete.append(n)
            drop_bytes += _size(os.path.join(DB, n))

    total = sum(_size(os.path.join(DB, n)) for n in names)
    return to_delete, total, drop_bytes


def clean(refresh_ok=True, quiet=False, protect=(), keep_days=KEEP_DAYS,
          keep_kline=KLINE_KEEP, dry_run=False, protect_mtime_within=0):
    """清理 database/。返回 (删除个数, 释放字节)。

    dry_run=True 只报告不删（--no-clean 之外还想看清单时用）。
    protect_mtime_within：见 collect()。
    """
    if not refresh_ok or not os.path.isdir(DB):
        return 0, 0
    to_delete, total, drop = collect(keep_days, keep_kline, protect,
                                     protect_mtime_within)
    if not to_delete:
        if not quiet:
            print(f"      database/ 无需清理（{total / 1e6:.0f} MB）")
        return 0, 0
    if dry_run:
        print(f"      [预演] 将删除 {len(to_delete)} 个文件，"
              f"释放 {drop / 1e6:.0f} MB（当前 {total / 1e6:.0f} MB）")
        return 0, 0

    removed = 0
    freed = 0
    for n in to_delete:
        p = os.path.join(DB, n)
        sz = _size(p)
        try:
            os.remove(p)
        except OSError:
            continue
        removed += 1
        freed += sz
    if not quiet:
        left = total - freed
        print(f"      已清理 database/：删除 {removed} 个文件，"
              f"释放 {freed / 1e6:.0f} MB（{total / 1e6:.0f} → {left / 1e6:.0f} MB）")
    return removed, freed
