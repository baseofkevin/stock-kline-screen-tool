"""kline_screen 命令行参数与入口。"""
import argparse
import sys

from .params import (CHG_HI, CHG_LO, K_VOL, MA60_GAP, TURN_MAX_N, TURN_MIN_N,
                     TURN_PCT)
from .pipeline import CliError, clean_db, run


def build_parser():
    ap = argparse.ArgumentParser(description="K线选股规则 v1.10（每周选股，K=5）")
    ap.add_argument("--date", help="基准交易日（默认最近交易日）")
    ap.add_argument("--n", type=int, default=4,
                    help="新鲜度窗口，必须 = 扫描周期−1（每周 K=5 用 4=默认；"
                         "每3日用 2；每日用 0）")
    ap.add_argument("--k-vol", type=float, default=K_VOL)
    ap.add_argument("--chg-lo", type=float, default=CHG_LO)
    ap.add_argument("--chg-hi", type=float, default=CHG_HI)
    ap.add_argument("--turn-pct", type=float, default=TURN_PCT,
                    help="换手硬过滤保留池内最低比例：默认 0（只排序标注）；"
                         "0.4=只留 Q1–Q2")
    ap.add_argument("--turn-min", type=int, default=TURN_MIN_N,
                    help="名单少于该数时自动放宽一档（默认 5）")
    ap.add_argument("--turn-max", type=int, default=TURN_MAX_N,
                    help="名单多于该数时自动收紧一档（默认 30）")
    ap.add_argument("--ma60-gap", type=float, default=MA60_GAP)
    ap.add_argument("--refresh", action="store_true",
                    help="强制重新下载原始数据（默认复用 database/ 下的已有文件）")
    ap.add_argument("--refresh-industry", action="store_true",
                    help="强制重建申万行业归属表（默认 30 天缓存，约 2~3 分钟）")
    ap.add_argument("--prune-kline", action="store_true",
                    help="兼容保留：kline 底稿清理已并入默认的 database/ 清理"
                         "（只留最新 KLINE_KEEP 份），该参数不再需要")
    ap.add_argument("--no-clean", action="store_true",
                    help="跑完后不清理 database/（默认会删掉下次用不上的旧缓存："
                         "kline 底稿留最新 2 份、逐日缓存留最近 14 天）")
    ap.add_argument("--clean-dry-run", action="store_true",
                    help="只打印 database/ 会删哪些文件，不真删、也不跑选股")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if getattr(a, "clean_dry_run", False):
        # 只预演清理，不跑选股：排查「删了会不会影响下次」时用
        from .common import dbclean
        dbclean.clean(dry_run=True, quiet=False)
        return 0
    try:
        code = run(a)
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except RuntimeError as e:
        # run() 里的数据类错误都故意用 RuntimeError 表达「宁可失败也不出不可信名单」。
        # 这里不打 traceback：用户要的是那句原因，不是调用栈。
        print(f"\n错误：{e}", file=sys.stderr)
        return 2
    # 清理放在这里而不是 run() 里：run() 也会被测试/外部脚本调用，
    # 那些场景用的是共享的 database/ fixture，不能删。
    clean_db(a)
    return code
