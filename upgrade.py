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
import io
import stat
import sqlite3
import tempfile
import zipfile
import urllib.request
import urllib.error
from datetime import datetime

# --- 配置 ---
REPO_OWNER = "smmya"
REPO_NAME = "pve-traffic-manager"
RAW_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/refs/heads/main"
ARCHIVE_URL = f"https://codeload.github.com/{REPO_OWNER}/{REPO_NAME}/zip/refs/heads/main"

SOURCE_FILES = [
    "manager.py",
    "monitor.py",
    "db.py",
    "pve.py",
    "config.py",
    "telegram_service.py",
    "telegram_bot.py",
    "requirements.txt",
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
        max_parts = max(3, len(p1), len(p2))
        while len(p1) < max_parts: p1.append(0)
        while len(p2) < max_parts: p2.append(0)
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
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = os.path.join(BASE_DIR, f"data.bak.{ts}.{suffix}")
        suffix += 1
    try:
        os.makedirs(backup_path)
        db_name = "traffic.db"
        source_db = os.path.join(DATA_DIR, db_name)
        with os.scandir(DATA_DIR) as entries:
            for entry in entries:
                if entry.name in {db_name, f"{db_name}-wal", f"{db_name}-shm", "monitor.lock"}:
                    continue
                target = os.path.join(backup_path, entry.name)
                if entry.is_dir(follow_symlinks=False):
                    shutil.copytree(entry.path, target)
                else:
                    shutil.copy2(entry.path, target, follow_symlinks=False)

        # SQLite 在线备份 API 可在 WAL 正在写入时生成一致快照。
        if os.path.exists(source_db):
            source_conn = sqlite3.connect(source_db, timeout=30)
            target_conn = sqlite3.connect(os.path.join(backup_path, db_name))
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
                source_conn.close()
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
        if os.path.isdir(backup_path):
            shutil.rmtree(backup_path, ignore_errors=True)
        return False


def download_source_files():
    """一次下载完整源码快照，避免逐文件下载得到混合版本。"""
    try:
        req = urllib.request.Request(
            f"{ARCHIVE_URL}?_cb={int(datetime.now().timestamp())}",
            headers={"User-Agent": "pve-traffic-manager-upgrader"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            archive = resp.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            result = {}
            names = zf.namelist()
            for filename in SOURCE_FILES:
                matches = [name for name in names if name.endswith(f"/{filename}")]
                if len(matches) != 1:
                    log(f"升级包中文件缺失或重复: {filename}", "ERROR")
                    return None
                result[filename] = zf.read(matches[0])
        return result
    except urllib.error.HTTPError as e:
        log(f"下载升级包失败 (HTTP {e.code})", "ERROR")
    except zipfile.BadZipFile:
        log("下载的升级包不是有效 ZIP 文件", "ERROR")
    except Exception as e:
        log(f"下载升级包失败: {e}", "ERROR")
    return None


def _version_from_content(content):
    try:
        for line in content.decode("utf-8").splitlines():
            if line.strip().startswith("VERSION="):
                return line.strip().split("=", 1)[1]
    except (AttributeError, UnicodeDecodeError):
        pass
    return None


def validate_source_files(source_files):
    """在替换本地文件前验证完整性和 Python 语法。"""
    if not source_files or set(source_files) != set(SOURCE_FILES):
        log("升级包文件不完整", "ERROR")
        return False
    for filename, content in source_files.items():
        if not content:
            log(f"升级包文件为空: {filename}", "ERROR")
            return False
        if filename.endswith(".py"):
            try:
                source = content.decode("utf-8")
                compile(source, filename, "exec")
            except (UnicodeDecodeError, SyntaxError) as exc:
                log(f"升级包语法校验失败 {filename}: {exc}", "ERROR")
                return False
    return True


def _atomic_write(filepath, content):
    """在目标目录写临时文件，再以 os.replace 原子替换。"""
    directory = os.path.dirname(filepath)
    mode = None
    if os.path.exists(filepath):
        mode = stat.S_IMODE(os.stat(filepath).st_mode)
    fd, temp_path = tempfile.mkstemp(prefix=".ptm-upgrade-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, filepath)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def install_source_files(source_files):
    """事务式安装源码；任一替换失败时恢复全部原文件。"""
    originals = {}
    installed = []
    for filename in SOURCE_FILES:
        filepath = os.path.join(BASE_DIR, filename)
        if os.path.exists(filepath):
            with open(filepath, "rb") as handle:
                originals[filename] = handle.read()
        else:
            originals[filename] = None

    try:
        for filename in SOURCE_FILES:
            _atomic_write(os.path.join(BASE_DIR, filename), source_files[filename])
            installed.append(filename)
            log(f"已更新: {filename}", "OK")
        return True
    except Exception as exc:
        log(f"写入升级文件失败: {exc}", "ERROR")
        rollback_errors = []
        for filename in reversed(installed):
            try:
                filepath = os.path.join(BASE_DIR, filename)
                if originals[filename] is None:
                    os.remove(filepath)
                else:
                    _atomic_write(filepath, originals[filename])
            except Exception as rollback_exc:
                rollback_errors.append(f"{filename}: {rollback_exc}")
        if rollback_errors:
            log(f"回滚失败: {'; '.join(rollback_errors)}", "ERROR")
        else:
            log("已恢复升级前的程序文件", "WARN")
        return False


def perform_upgrade(force=False):
    log("=" * 50)
    log("PVE 流量控制管理器 - 升级程序", "INFO")
    log("=" * 50)

    local_ver = get_local_version()
    log(f"本地版本: v{local_ver}")

    log("正在下载并校验升级包...", "INFO")
    source_files = download_source_files()
    if not validate_source_files(source_files):
        log("无法取得有效升级包，升级中止", "ERROR")
        return False

    remote_ver = _version_from_content(source_files["VERSION"])
    if remote_ver is None:
        log("升级包缺少有效版本信息，升级中止", "ERROR")
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

    log("-" * 50)
    if not install_source_files(source_files):
        log("升级失败，程序文件已尝试回滚；数据备份保持不变", "ERROR")
        return False

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
