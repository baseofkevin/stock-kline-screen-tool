#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kline_screen.py —— K 线选股入口。

规则与输出说明见 README.md。
"""
import sys

from kline_screen.cli import main

if __name__ == "__main__":
    sys.exit(main())
