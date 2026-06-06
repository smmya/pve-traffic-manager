#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - 一键升级脚本
由 manager.py 调用，也可独立运行

用法:
    python3 upgrade.py              # 检查并升级到最新版
    python3 upgrade.py --check      # 仅检查是否有新版本
    python3 upgrade.py --force      # 强制升级（即使版本相同）
"""

import os
import sys
import shutil
import urllib.request
import urllib.error
from datetime import datetime

# --- 配置 ---
REPO_OWNER = "smmya"
REPO_NAME = "pve-traffic-manager"
RAW_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/refs/heads/main"

SOURCE_FILES = [
    "manager.py",
    "monitor.py",
    "db.py",
    "pve.py",
    "config.py",
    "upgrade.py",
    "VERSION",
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": " [*]", "OK": " [+]", "WARN": " [!]", "ERROR": " [x]"}.get(level, " [*]")
    print(f"[{ts}]{prefix} {msg}")


def get_local_version():
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
    url = f"{RAW_URL}/VERSION?_cb={int(datetime.now().timestamp())}"
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
    try:
        p1 = [int(x) for x in v1.strip().split(".")]
        p2 = [int(x) for x in v2.strip().split(".")]
        while len(p1) < 3: p1.append(0)
        while len(p2) < 3: p2.append(0)
        for a, b in zip(p1, p2):
            if a > b: return 1
            if a < b: return -1
        return 0
    except (ValueError, AttributeError):
        return 0


def backup_data():
    if not os.path.exists(DATA_DIR):
        log("data 目录不存在，跳过备份", "INFO")
        return True
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BASE_DIR, f"data.bak.{ts}")
    try:
        shutil.copytree(DATA_DIR, backup_path)
        log(f"数据已备份到: {backup_path}", "OK")
        # 清理旧备份，保留最近 5 个
        backups = sorted([d for d in os.listdir(BASE_DIR) if d.startswith("data.bak.") and os.path.isdir(os.path.join(BASE_DIR, d))])
        while len(backups) > 5:
            old = os.path.join(BASE_DIR, backups.pop(0))
            shutil.rmtree(old)
            log(f"清理旧备份: {old}", "INFO")
        return True
    except Exception as e:
        log(f"备份失败: {e}", "ERROR")
        return False


def download_file(filename):
    url = f"{RAW_URL}/{filename}?_cb={int(datetime.now().timestamp())}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pve-traffic-manager-upgrader"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"远程文件不存在: {filename}", "WARN")
        else:
            log(f"下载失败 {filename} (HTTP {e.code})", "ERROR")
        return None
    except Exception as e:
        log(f"下载失败 {filename}: {e}", "ERROR")
        return None


def perform_upgrade(force=False):
    log("=" * 50)
    log("PVE 流量控制管理器 - 升级程序", "INFO")
    log("=" * 50)

    local_ver = get_local_version()
    log(f"本地版本: v{local_ver}")

    remote_ver = get_remote_version()
    if remote_ver is None:
        log("无法获取远程版本信息，升级中止", "ERROR")
        return False

    log(f"远程版本: v{remote_ver}")

    cmp = compare_versions(remote_ver, local_ver)
    if cmp <= 0 and not force:
        if cmp == 0:
            log("当前已是最新版本，无需升级", "OK")
        else:
            log(f"本地版本 (v{local_ver}) 高于远程版本 (v{remote_ver})，无需升级", "WARN")
        return True
    elif cmp <= 0 and force:
        log("强制升级模式，跳过版本检查", "WARN")

    print()
    print(f"  将升级: v{local_ver} -> v{remote_ver}")
    print(f"  将替换以下文件: {', '.join(SOURCE_FILES)}")
    print(f"  data/ 目录将被保留（升级前会自动备份）")
    print()
    confirm = input("  确认升级? (y/N): ").strip().lower()
    if confirm != "y":
        log("用户取消升级", "INFO")
        return False

    log("正在备份数据...", "INFO")
    if not backup_data():
        log("数据备份失败，升级中止", "ERROR")
        return False

    log("正在下载更新...", "INFO")
    ok, fail = 0, []
    for fname in SOURCE_FILES:
        content = download_file(fname)
        if content is None:
            fail.append(fname)
            continue
        filepath = os.path.join(BASE_DIR, fname)
        try:
            with open(filepath, "wb") as f:
                f.write(content)
            log(f"已更新: {fname}", "OK")
            ok += 1
        except Exception as e:
            log(f"写入失败 {fname}: {e}", "ERROR")
            fail.append(fname)

    log("-" * 50)
    if fail:
        log(f"升级部分完成 ({ok}/{len(SOURCE_FILES)} 个文件成功)", "WARN")
        log(f"失败文件: {', '.join(fail)}", "WARN")
        log("数据目录已备份，可手动恢复", "INFO")
        return False
    else:
        log(f"升级成功! v{local_ver} -> v{remote_ver}", "OK")
        print()
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
