# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - 配置文件
"""

import os
import sys

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据库路径
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'traffic.db')

# PVE 节点名称（单节点默认 localhost）
PVE_NODE = 'localhost'

# 默认监控间隔（分钟）
DEFAULT_MONITOR_INTERVAL = 5

# 自动创建数据目录
os.makedirs(DATA_DIR, exist_ok=True)

# Python 解释器路径（用于 crontab 建议）
PYTHON_PATH = sys.executable

# 脚本路径（用于 crontab 建议）
MONITOR_SCRIPT = os.path.join(BASE_DIR, 'monitor.py')
