#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - 一键升级脚本
用法:
    python3 upgrade.py              # 检查并升级到最新版
    python3 upgrade.py --check      # 仅检查是否有新版本
    python3 upgrade.py --force      # 强制升级（即使版本相同）
"""

import os
import sys
import shutil
import hashlib
import urllib.request
import urllib.error
from datetime import datetime

# --- 配置 ---
REPO_OWNER = "smmya"
REPO_NAME = "pve-traffic-manager"
REPO_URL = f"https://github.com/{REPO_OWNER}/{REPO_NAME}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main"

# 需要更新的源文件（不含 data 目录）
SOURCE_FILES = [
    "manager.py",
    "monitor.py",
    "db.py",
    "pve.py",
    "config.py",
    "VERSION",
]

# 当前脚本所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")


# ============================================================
#  工具函数
# ============================================================

def log(msg, level="INFO"):
    """带时间戳日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": " [*]", "OK": " [+]", "WARN": " [!]", "ERROR": " [x]"}.get(level, " [*]")
    print(f"[{ts}]{prefix} {msg}")


def get_local_version():
    """读取本地版本号"""
    version_file = os.path.join(BASE_DIR, "VERSION")
    if not os.path.exists(version_file):
        return "0.0.0"
    with open(version_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("VERSION="):
                return line.split("=", 1)[1]
    return "0.0.0"


def get_remote_version():
    """从 GitHub 获取远程最新版本号"""
    url = f"{RAW_URL}/VERSION"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pve-traffic-manager-upgrader"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("VERSION="):
                    return line.split("=", 1)[1]
    except urllib.error.HTTPError as e:
        log(f"无法获取远程版本 (HTTP {e.code})", "WARN")
    except urllib.error.URLError as e:
        log(f"网络连接失败: {e.reason}", "WARN")
    except Exception as e:
        log(f"获取远程版本异常: {e}", "WARN")
    return None


def compare_versions(v1, v2):
    """
    比较语义版本号，返回: 1 (v1 > v2), -1 (v1 < v2), 0 (相等)
    格式: major.minor.patch
    """
    try:
        parts1 = [int(x) for x in v1.strip().split(".")]
        parts2 = [int(x) for x in v2.strip().split(".")]
        # 补齐到 3 位
        while len(parts1) < 3:
            parts1.append(0)
        while len(parts2) < 3:
            parts2.append(0)
        for a, b in zip(parts1, parts2):
            if a > b:
                return 1
            if a < b:
                return -1
        return 0
    except (ValueError, AttributeError):
        return 0


def backup_data():
    """备份 data 目录"""
    if not os.path.exists(DATA_DIR):
        log("data 目录不存在，跳过备份", "INFO")
        return True

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BASE_DIR, f"data.bak.{ts}")

    try:
        shutil.copytree(DATA_DIR, backup_path)
        log(f"数据已备份到: {backup_path}", "OK")

        # 清理旧备份（保留最近 5 个）
        backups = sorted([
            d for d in os.listdir(BASE_DIR)
            if d.startswith("data.bak.") and os.path.isdir(os.path.join(BASE_DIR, d))
        ])
        while len(backups) > 5:
            old = backups.pop(0)
            old_path = os.path.join(BASE_DIR, old)
            shutil.rmtree(old_path)
            log(f"清理旧备份: {old}", "INFO")

        return True
    except Exception as e:
        log(f"备份失败: {e}", "ERROR")
        return False


def download_file(filename):
    """从 GitHub 下载单个文件"""
    url = f"{RAW_URL}/{filename}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pve-traffic-manager-upgrader"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
            return content
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"远程文件不存在: {filename}", "WARN")
        else:
            log(f"下载失败 {filename} (HTTP {e.code})", "ERROR")
        return None
    except Exception as e:
        log(f"下载失败 {filename}: {e}", "ERROR")
        return None


def file_sha256(filepath):
    """计算文件 SHA256"""
    if not os.path.exists(filepath):
        return ""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def perform_upgrade(force=False):
    """执行升级"""
    log("=" * 50)
    log("PVE 流量控制管理器 - 升级程序", "INFO")
    log("=" * 50)

    # 1. 获取版本信息
    local_ver = get_local_version()
    log(f"本地版本: v{local_ver}")

    remote_ver = get_remote_version()
    if remote_ver is None:
        log("无法获取远程版本信息，升级中止", "ERROR")
        return False

    log(f"远程版本: v{remote_ver}")

    # 2. 版本比较
    cmp = compare_versions(remote_ver, local_ver)
    if cmp <= 0 and not force:
        if cmp == 0:
            log("当前已是最新版本，无需升级", "OK")
        else:
            log(f"本地版本 (v{local_ver}) 高于远程版本 (v{remote_ver})，无需升级", "WARN")
        log("如需强制升级请使用: python3 upgrade.py --force", "INFO")
        return True
    elif cmp <= 0 and force:
        log("强制升级模式，跳过版本检查", "WARN")

    # 3. 确认
    print()
    print(f"  将升级: v{local_ver} -> v{remote_ver}")
    print(f"  仓库地址: {REPO_URL}")
    print(f"  将替换以下文件: {', '.join(SOURCE_FILES)}")
    print(f"  data/ 目录将被保留（升级前会自动备份）")
    print()
    confirm = input("  确认升级? (y/N): ").strip().lower()
    if confirm != "y":
        log("用户取消升级", "INFO")
        return False

    # 4. 备份数据
    log("正在备份数据...", "INFO")
    if not backup_data():
        log("数据备份失败，升级中止", "ERROR")
        return False

    # 5. 下载并替换文件
    log("正在下载更新...", "INFO")
    success_count = 0
    failed_files = []

    for filename in SOURCE_FILES:
        content = download_file(filename)
        if content is None:
            failed_files.append(filename)
            continue

        filepath = os.path.join(BASE_DIR, filename)
        try:
            with open(filepath, "wb") as f:
                f.write(content)
            log(f"已更新: {filename}", "OK")
            success_count += 1
        except Exception as e:
            log(f"写入失败 {filename}: {e}", "ERROR")
            failed_files.append(filename)

    # 6. 结果
    log("-" * 50)
    if failed_files:
        log(f"升级部分完成 ({success_count}/{len(SOURCE_FILES)} 个文件成功)", "WARN")
        log(f"失败文件: {', '.join(failed_files)}", "WARN")
        log("数据目录已备份，可手动恢复", "INFO")
        return False
    else:
        log(f"升级成功! v{local_ver} -> v{remote_ver}", "OK")
        log("请重新运行程序以使用新版本", "INFO")
        return True


def main():
    force = "--force" in sys.argv
    check_only = "--check" in sys.argv

    if check_only:
        local_ver = get_local_version()
        remote_ver = get_remote_version()
        print(f"本地版本: v{local_ver}")
        print(f"远程版本: v{remote_ver}" if remote_ver else "远程版本: 获取失败")
        if remote_ver:
            cmp = compare_versions(remote_ver, local_ver)
            if cmp > 0:
                print(f"\n有新版本可用! 运行 'python3 upgrade.py' 进行升级")
            else:
                print("\n当前已是最新版本")
        return

    try:
        success = perform_upgrade(force=force)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        log("\n升级被中断", "WARN")
        sys.exit(1)
    except Exception as e:
        log(f"升级过程异常: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
