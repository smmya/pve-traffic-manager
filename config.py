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

# 项目私有 Python 依赖目录，避免修改 PVE/Debian 的系统 Python 环境。
PYTHON_PACKAGES_DIR = os.path.join(BASE_DIR, '.ptm-packages')
if PYTHON_PACKAGES_DIR not in sys.path:
    sys.path.insert(0, PYTHON_PACKAGES_DIR)

# 后台监控单实例锁
MONITOR_LOCK_PATH = os.path.join(DATA_DIR, 'monitor.lock')

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

# Telegram Bot 长轮询脚本与 systemd 单元
TELEGRAM_BOT_SCRIPT = os.path.join(BASE_DIR, 'telegram_bot.py')
TELEGRAM_SERVICE_NAME = 'pve-traffic-manager-bot.service'
TELEGRAM_SERVICE_PATH = os.path.join('/etc/systemd/system', TELEGRAM_SERVICE_NAME)
