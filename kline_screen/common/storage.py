"""JSON / CSV 原子读写。"""
import csv
import json
import os


def jload(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def jdump(path, obj):
    """原子写 JSON：先写同目录 .tmp，fsync 后 os.replace（同盘 rename 是原子的）。

    这样要么保留旧文件、要么得到完整新文件；即使进程在写入中途被杀，也不会
    留下半截 JSON 被 jload() 的 `except: return default` 静默当成空缓存。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_csv_atomic(path, header, rows):
    """原子写 CSV（同上）。writerows 一次写全，避免逐行 flush 的额外开销。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def count_csv_rows(path):
    """数据行数（不含表头）。用于收尾校验 CSV 与名单条数一致。"""
    with open(path, encoding="utf-8-sig") as fh:
        return sum(1 for _ in fh) - 1
