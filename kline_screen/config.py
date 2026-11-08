"""项目路径与外部命令位置。"""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HERE, "database")
CSVDIR = os.path.join(HERE, "csv_output")
W = os.path.expanduser("~/.local/bin/westock")
