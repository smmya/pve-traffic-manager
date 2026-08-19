# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - 数据库操作层
"""

import sqlite3
import os
import math
import datetime
import ipaddress
from config import DB_PATH, DATA_DIR, MONITOR_LOCK_PATH

try:
    import fcntl
except ImportError:  # pragma: no cover - PVE/Linux 环境始终可用
    fcntl = None


BOOT_TIME_TOLERANCE_SECONDS = 60


def _is_positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def get_conn():
    """获取数据库连接"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        # 数据库现在包含 Bot Token；在 Linux/PVE 上限制为当前用户可读写。
        os.chmod(DATA_DIR, 0o700)
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def acquire_monitor_lock():
    """非阻塞获取监控/采样进程锁；None 表示已有采样正在执行。"""
    os.makedirs(os.path.dirname(MONITOR_LOCK_PATH), exist_ok=True)
    handle = open(MONITOR_LOCK_PATH, 'a+', encoding='utf-8')
    if fcntl is None:
        return handle
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None


def release_monitor_lock(handle):
    if handle is None:
        return
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def init_db():
    """初始化数据库表结构"""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            traffic_limit_mb REAL NOT NULL,
            notify_cmd      TEXT DEFAULT '',
            all_shutdown_notified INTEGER NOT NULL DEFAULT 0
                CHECK(all_shutdown_notified IN (0,1)),
            created_at      TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS group_vms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER NOT NULL,
            vm_id       INTEGER NOT NULL,
            vm_type     TEXT NOT NULL CHECK(vm_type IN ('qemu','lxc')),
            vm_name     TEXT,
            added_at    TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            UNIQUE(group_id, vm_id, vm_type)
        );

        CREATE TABLE IF NOT EXISTS traffic_summary (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vm_id           INTEGER NOT NULL,
            vm_type         TEXT NOT NULL,
            group_id        INTEGER NOT NULL,
            last_reset      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            total_in_mb     REAL DEFAULT 0,
            total_out_mb    REAL DEFAULT 0,
            warning_sent    INTEGER NOT NULL DEFAULT 0 CHECK(warning_sent IN (0,1)),
            shutdown_notified INTEGER NOT NULL DEFAULT 0 CHECK(shutdown_notified IN (0,1)),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            UNIQUE(vm_id, vm_type, group_id)
        );

        CREATE TABLE IF NOT EXISTS traffic_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            vm_id           INTEGER NOT NULL,
            vm_type         TEXT NOT NULL,
            netin_bytes     INTEGER DEFAULT 0,
            netout_bytes    INTEGER DEFAULT 0,
            delta_in_bytes  INTEGER DEFAULT 0,
            delta_out_bytes INTEGER DEFAULT 0,
            boot_time       INTEGER,
            timestamp       TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS traffic_resets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER,
            vm_id       INTEGER,
            vm_type     TEXT,
            reset_at    TEXT DEFAULT (datetime('now','localtime')),
            reason      TEXT
        );

        CREATE TABLE IF NOT EXISTS action_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            action      TEXT NOT NULL,
            target_type TEXT,
            target_id   INTEGER,
            detail      TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS shutdown_state (
            vm_id                   INTEGER NOT NULL,
            vm_type                 TEXT NOT NULL CHECK(vm_type IN ('qemu','lxc')),
            requested_at            TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            boot_time_at_request    INTEGER,
            group_id                INTEGER,
            stopped_seen            INTEGER NOT NULL DEFAULT 0 CHECK(stopped_seen IN (0,1)),
            PRIMARY KEY (vm_id, vm_type)
        );

        CREATE TABLE IF NOT EXISTS telegram_settings (
            id              INTEGER PRIMARY KEY CHECK(id = 1),
            bot_token       TEXT NOT NULL DEFAULT '',
            chat_id         TEXT NOT NULL DEFAULT '',
            enabled         INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            warning_percent REAL NOT NULL DEFAULT 80,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        INSERT OR IGNORE INTO telegram_settings
            (id, bot_token, chat_id, enabled, warning_percent)
            VALUES (1, '', '', 0, 80);

        CREATE TABLE IF NOT EXISTS network_check_settings (
            id                INTEGER PRIMARY KEY CHECK(id = 1),
            enabled           INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            targets           TEXT NOT NULL DEFAULT '',
            interval_hours    REAL NOT NULL DEFAULT 6,
            running           INTEGER NOT NULL DEFAULT 0 CHECK(running IN (0,1)),
            last_started_at   TEXT,
            last_completed_at TEXT,
            last_result       TEXT NOT NULL DEFAULT '',
            updated_at        TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        INSERT OR IGNORE INTO network_check_settings
            (id, enabled, targets, interval_hours, running)
            VALUES (1, 0, '', 6, 0);

        CREATE TABLE IF NOT EXISTS network_check_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vm_id       INTEGER NOT NULL,
            vm_name     TEXT,
            target      TEXT NOT NULL,
            success     INTEGER NOT NULL CHECK(success IN (0,1)),
            detail      TEXT,
            checked_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_traffic_logs_vm_latest
            ON traffic_logs(vm_id, vm_type, id DESC);
        CREATE INDEX IF NOT EXISTS idx_action_logs_vm_latest
            ON action_logs(action, target_type, target_id, id DESC);
        CREATE INDEX IF NOT EXISTS idx_traffic_resets_vm_latest
            ON traffic_resets(vm_id, vm_type, reason, id DESC);
        CREATE INDEX IF NOT EXISTS idx_network_check_logs_latest
            ON network_check_logs(id DESC);
    ''')

    # 从旧版本原地升级数据库。CREATE TABLE IF NOT EXISTS 不会补充新列。
    traffic_columns = {
        row['name'] for row in cursor.execute("PRAGMA table_info(traffic_logs)").fetchall()
    }
    if 'boot_time' not in traffic_columns:
        cursor.execute("ALTER TABLE traffic_logs ADD COLUMN boot_time INTEGER")

    summary_columns = {
        row['name'] for row in cursor.execute("PRAGMA table_info(traffic_summary)").fetchall()
    }
    if 'warning_sent' not in summary_columns:
        cursor.execute(
            "ALTER TABLE traffic_summary ADD COLUMN warning_sent INTEGER NOT NULL DEFAULT 0"
        )
    if 'shutdown_notified' not in summary_columns:
        cursor.execute(
            "ALTER TABLE traffic_summary ADD COLUMN shutdown_notified INTEGER NOT NULL DEFAULT 0"
        )

    group_columns = {
        row['name'] for row in cursor.execute("PRAGMA table_info(groups)").fetchall()
    }
    if 'all_shutdown_notified' not in group_columns:
        cursor.execute(
            "ALTER TABLE groups ADD COLUMN all_shutdown_notified INTEGER NOT NULL DEFAULT 0"
        )

    shutdown_columns = {
        row['name'] for row in cursor.execute("PRAGMA table_info(shutdown_state)").fetchall()
    }
    if 'group_id' not in shutdown_columns:
        cursor.execute("ALTER TABLE shutdown_state ADD COLUMN group_id INTEGER")

    conn.commit()
    conn.close()


# ============================================================
#  组 (Groups) 相关操作
# ============================================================

def create_group(name, traffic_limit_mb, notify_cmd=''):
    """创建管理组"""
    if not str(name).strip():
        return False, "组名不能为空"
    if not _is_positive_number(traffic_limit_mb):
        return False, "流量限额必须为正数"
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO groups (name, traffic_limit_mb, notify_cmd) VALUES (?, ?, ?)",
            (name, traffic_limit_mb, notify_cmd)
        )
        conn.commit()
        gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return True, gid
    except sqlite3.IntegrityError:
        return False, "组名已存在"
    finally:
        conn.close()


def get_all_groups():
    """获取所有组"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, name, traffic_limit_mb, notify_cmd,
                  all_shutdown_notified, created_at
           FROM groups ORDER BY id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_group_by_id(group_id):
    """根据ID获取组"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_group_by_name(name):
    """根据名称获取组"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM groups WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_group(group_id, name=None, traffic_limit_mb=None, notify_cmd=None):
    """更新组信息"""
    conn = get_conn()
    group = get_group_by_id(group_id)
    if not group:
        conn.close()
        return False, "组不存在"

    new_name = name if name is not None else group['name']
    new_limit = traffic_limit_mb if traffic_limit_mb is not None else group['traffic_limit_mb']
    new_notify = notify_cmd if notify_cmd is not None else group['notify_cmd']
    if not str(new_name).strip():
        conn.close()
        return False, "组名不能为空"
    if not _is_positive_number(new_limit):
        conn.close()
        return False, "流量限额必须为正数"
    reset_all_shutdown = (
        traffic_limit_mb is not None
        and float(new_limit) != float(group['traffic_limit_mb'])
    )

    try:
        conn.execute(
            """UPDATE groups
               SET name=?, traffic_limit_mb=?, notify_cmd=?,
                   all_shutdown_notified=CASE WHEN ? THEN 0 ELSE all_shutdown_notified END
               WHERE id=?""",
            (new_name, new_limit, new_notify, 1 if reset_all_shutdown else 0, group_id)
        )
        conn.commit()
        return True, "更新成功"
    except sqlite3.IntegrityError:
        return False, "组名已存在"
    finally:
        conn.close()


def delete_group(group_id):
    """删除组（级联删除 group_vms 和 traffic_summary）"""
    conn = get_conn()
    with conn:
        affected_vms = conn.execute(
            "SELECT vm_id, vm_type FROM group_vms WHERE group_id=?",
            (group_id,)
        ).fetchall()
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        conn.execute("DELETE FROM shutdown_state WHERE group_id=?", (group_id,))
        for vm in affected_vms:
            remaining = conn.execute(
                """SELECT 1 FROM group_vms
                   WHERE vm_id=? AND vm_type=? LIMIT 1""",
                (vm['vm_id'], vm['vm_type'])
            ).fetchone()
            if not remaining:
                conn.execute(
                    "DELETE FROM shutdown_state WHERE vm_id=? AND vm_type=?",
                    (vm['vm_id'], vm['vm_type'])
                )
    conn.close()
    return True


# ============================================================
#  组内虚拟机 (group_vms) 相关操作
# ============================================================

def _calculate_deltas(last_log, netin_bytes, netout_bytes, boot_time=None):
    """根据上一条采样计算两个方向各自的增量。"""
    if last_log is None:
        return 0, 0

    last_boot_time = last_log.get('boot_time')
    restarted = (
        boot_time is not None
        and last_boot_time is not None
        and abs(int(boot_time) - int(last_boot_time)) > BOOT_TIME_TOLERANCE_SECONDS
    )
    if restarted:
        return netin_bytes, netout_bytes

    # 两个计数器可能独立回绕，不能因一个方向变小而重置另一个方向。
    delta_in = netin_bytes if netin_bytes < last_log['netin_bytes'] else netin_bytes - last_log['netin_bytes']
    delta_out = netout_bytes if netout_bytes < last_log['netout_bytes'] else netout_bytes - last_log['netout_bytes']
    return delta_in, delta_out


def add_vm_to_group(group_id, vm_id, vm_type, vm_name='', initial_in_mb=0,
                    initial_out_mb=0, baseline_in_bytes=None,
                    baseline_out_bytes=None, boot_time=None):
    """
    将虚拟机加入组。

    若提供当前 PVE 计数器，会在同一事务中先把自上次采样以来的增量
    补入已有组，再为新组建立汇总和新的采样基线，避免漏计或重复计费。
    """
    conn = get_conn()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # 先插入成员关系；重复添加会在修改采样基线前失败并回滚。
            conn.execute(
                "INSERT INTO group_vms (group_id, vm_id, vm_type, vm_name) VALUES (?, ?, ?, ?)",
                (group_id, vm_id, vm_type, vm_name)
            )
            conn.execute(
                "UPDATE groups SET all_shutdown_notified=0 WHERE id=?",
                (group_id,)
            )
            if vm_name:
                conn.execute(
                    """UPDATE group_vms SET vm_name=?
                       WHERE vm_id=? AND vm_type=?""",
                    (vm_name, vm_id, vm_type)
                )

            if baseline_in_bytes is not None and baseline_out_bytes is not None:
                baseline_in_bytes = max(0, int(baseline_in_bytes))
                baseline_out_bytes = max(0, int(baseline_out_bytes))
                last_row = conn.execute(
                    """SELECT * FROM traffic_logs
                       WHERE vm_id=? AND vm_type=? ORDER BY id DESC LIMIT 1""",
                    (vm_id, vm_type)
                ).fetchone()
                last_log = dict(last_row) if last_row else None
                delta_in, delta_out = _calculate_deltas(
                    last_log, baseline_in_bytes, baseline_out_bytes, boot_time
                )
                if last_log is not None and (delta_in > 0 or delta_out > 0):
                    conn.execute(
                        """UPDATE traffic_summary
                           SET total_in_mb = total_in_mb + ?,
                               total_out_mb = total_out_mb + ?
                           WHERE vm_id=? AND vm_type=?""",
                        (delta_in / (1024 * 1024), delta_out / (1024 * 1024),
                         vm_id, vm_type)
                    )
                conn.execute(
                    """INSERT INTO traffic_logs
                       (vm_id, vm_type, netin_bytes, netout_bytes,
                        delta_in_bytes, delta_out_bytes, boot_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (vm_id, vm_type, baseline_in_bytes, baseline_out_bytes,
                     delta_in, delta_out, boot_time)
                )

            conn.execute(
                """INSERT INTO traffic_summary
                   (vm_id, vm_type, group_id, total_in_mb, total_out_mb)
                   VALUES (?, ?, ?, ?, ?)""",
                (vm_id, vm_type, group_id, initial_in_mb, initial_out_mb)
            )
        return True, "加入成功"
    except sqlite3.IntegrityError as exc:
        if 'UNIQUE constraint failed: group_vms' in str(exc):
            return False, "该虚拟机已在此组中"
        if 'FOREIGN KEY constraint failed' in str(exc):
            return False, "目标组不存在"
        return False, f"数据校验失败: {exc}"
    finally:
        conn.close()


def remove_vm_from_group(group_id, vm_id, vm_type):
    """从组中移除虚拟机"""
    conn = get_conn()
    conn.execute(
        "DELETE FROM group_vms WHERE group_id=? AND vm_id=? AND vm_type=?",
        (group_id, vm_id, vm_type)
    )
    conn.execute(
        "DELETE FROM traffic_summary WHERE group_id=? AND vm_id=? AND vm_type=?",
        (group_id, vm_id, vm_type)
    )
    conn.execute(
        """DELETE FROM shutdown_state
           WHERE vm_id=? AND vm_type=? AND group_id=?""",
        (vm_id, vm_type, group_id)
    )
    # VM 已不属于任何组时，清理残留的关机等待状态。
    remaining = conn.execute(
        "SELECT 1 FROM group_vms WHERE vm_id=? AND vm_type=? LIMIT 1",
        (vm_id, vm_type)
    ).fetchone()
    if not remaining:
        conn.execute(
            "DELETE FROM shutdown_state WHERE vm_id=? AND vm_type=?",
            (vm_id, vm_type)
        )
    conn.commit()
    conn.close()
    return True


def get_group_vms(group_id):
    """获取组内所有虚拟机"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, group_id, vm_id, vm_type, vm_name, added_at FROM group_vms WHERE group_id=? ORDER BY vm_id",
        (group_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_vm_groups(vm_id, vm_type):
    """获取虚拟机所属的所有组"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT gv.*, g.name as group_name, g.traffic_limit_mb
           FROM group_vms gv
           JOIN groups g ON gv.group_id = g.id
           WHERE gv.vm_id=? AND gv.vm_type=?""",
        (vm_id, vm_type)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_managed_vms():
    """获取所有已管理的虚拟机"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT gv.vm_id, gv.vm_type, MAX(gv.vm_name) AS vm_name,
                GROUP_CONCAT(g.name, ', ') as group_names
           FROM group_vms gv
           JOIN groups g ON gv.group_id = g.id
           GROUP BY gv.vm_id, gv.vm_type
           ORDER BY gv.vm_id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  流量汇总 (traffic_summary) 相关操作
# ============================================================

def get_vm_traffic_summary(vm_id, vm_type, group_id):
    """获取虚拟机在指定组中的流量汇总"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM traffic_summary WHERE vm_id=? AND vm_type=? AND group_id=?",
        (vm_id, vm_type, group_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_traffic_summary(vm_id, vm_type, group_id, delta_in_bytes, delta_out_bytes):
    """更新流量汇总（累加增量）"""
    conn = get_conn()
    delta_in_mb = delta_in_bytes / (1024 * 1024)
    delta_out_mb = delta_out_bytes / (1024 * 1024)

    conn.execute(
        """UPDATE traffic_summary
           SET total_in_mb = total_in_mb + ?,
               total_out_mb = total_out_mb + ?
           WHERE vm_id=? AND vm_type=? AND group_id=?""",
        (delta_in_mb, delta_out_mb, vm_id, vm_type, group_id)
    )
    conn.commit()
    conn.close()


def get_group_traffic_overview(group_id):
    """获取组内所有VM的流量概览"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT ts.*, gv.vm_name
           FROM traffic_summary ts
           JOIN group_vms gv ON ts.vm_id=gv.vm_id AND ts.vm_type=gv.vm_type AND ts.group_id=gv.group_id
           WHERE ts.group_id=?
           ORDER BY ts.vm_id""",
        (group_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reset_group_traffic(group_id):
    """重置组内所有VM的流量"""
    conn = get_conn()
    now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 记录重置日志
    vms = conn.execute(
        "SELECT vm_id, vm_type FROM group_vms WHERE group_id=?", (group_id,)
    ).fetchall()
    for vm in vms:
        conn.execute(
            "INSERT INTO traffic_resets (group_id, vm_id, vm_type, reason) VALUES (?, ?, ?, 'manual')",
            (group_id, vm['vm_id'], vm['vm_type'])
        )

    # 重置流量
    conn.execute(
        """UPDATE traffic_summary
           SET total_in_mb=0, total_out_mb=0, last_reset=?,
               warning_sent=0, shutdown_notified=0
           WHERE group_id=?""",
        (now, group_id)
    )
    conn.execute(
        "UPDATE groups SET all_shutdown_notified=0 WHERE id=?", (group_id,)
    )
    conn.execute("DELETE FROM shutdown_state WHERE group_id=?", (group_id,))
    conn.commit()
    conn.close()
    return True


def reset_vm_traffic(vm_id, vm_type, group_id):
    """重置单台虚拟机在指定组中的流量"""
    conn = get_conn()
    now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn.execute(
        "INSERT INTO traffic_resets (group_id, vm_id, vm_type, reason) VALUES (?, ?, ?, 'manual')",
        (group_id, vm_id, vm_type)
    )
    conn.execute(
        """UPDATE traffic_summary
           SET total_in_mb=0, total_out_mb=0, last_reset=?,
               warning_sent=0, shutdown_notified=0
           WHERE vm_id=? AND vm_type=? AND group_id=?""",
        (now, vm_id, vm_type, group_id)
    )
    conn.execute(
        "UPDATE groups SET all_shutdown_notified=0 WHERE id=?", (group_id,)
    )
    conn.execute(
        """DELETE FROM shutdown_state
           WHERE vm_id=? AND vm_type=? AND group_id=?""",
        (vm_id, vm_type, group_id)
    )
    conn.commit()
    conn.close()
    return True


def get_last_shutdown_for_vm(vm_id):
    """获取某台VM最近一次关机记录（用于判断是否为PTM超限关机）"""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM action_logs
           WHERE action='shutdown' AND target_type='vm' AND target_id=?
           ORDER BY id DESC LIMIT 1""",
        (vm_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_last_auto_reset_for_vm(vm_id):
    """获取某台VM最近一次自动重置记录（用于防止重复重置）"""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM traffic_resets
           WHERE vm_id=? AND reason='auto_restart'
           ORDER BY id DESC LIMIT 1""",
        (vm_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_traffic_overview():
    """获取所有组的流量概览"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT g.id as group_id, g.name as group_name, g.traffic_limit_mb,
                   COUNT(gv.id) as vm_count,
                   COALESCE(SUM(ts.total_in_mb + ts.total_out_mb), 0) as total_traffic
            FROM groups g
            LEFT JOIN group_vms gv ON g.id = gv.group_id
            LEFT JOIN traffic_summary ts ON gv.vm_id = ts.vm_id AND gv.vm_type = ts.vm_type AND ts.group_id = g.id
            GROUP BY g.id
            ORDER BY g.id"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  流量日志 (traffic_logs) 相关操作
# ============================================================

def insert_traffic_log(vm_id, vm_type, netin_bytes, netout_bytes, delta_in_bytes,
                       delta_out_bytes, boot_time=None):
    """插入流量日志"""
    conn = get_conn()
    conn.execute(
        """INSERT INTO traffic_logs
           (vm_id, vm_type, netin_bytes, netout_bytes, delta_in_bytes,
            delta_out_bytes, boot_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (vm_id, vm_type, netin_bytes, netout_bytes, delta_in_bytes,
         delta_out_bytes, boot_time)
    )
    conn.commit()
    conn.close()


def get_last_traffic_log(vm_id, vm_type):
    """获取虚拟机最后一次流量记录（用于计算 delta）"""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM traffic_logs
           WHERE vm_id=? AND vm_type=?
           ORDER BY id DESC LIMIT 1""",
        (vm_id, vm_type)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def record_traffic_sample(vm_id, vm_type, netin_bytes, netout_bytes, boot_time=None):
    """原子地记录采样并把增量累加到该 VM 所属的全部组。"""
    netin_bytes = max(0, int(netin_bytes))
    netout_bytes = max(0, int(netout_bytes))
    boot_time = int(boot_time) if boot_time is not None else None

    conn = get_conn()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM traffic_logs
                   WHERE vm_id=? AND vm_type=? ORDER BY id DESC LIMIT 1""",
                (vm_id, vm_type)
            ).fetchone()
            last_log = dict(row) if row else None
            delta_in, delta_out = _calculate_deltas(
                last_log, netin_bytes, netout_bytes, boot_time
            )
            conn.execute(
                """INSERT INTO traffic_logs
                   (vm_id, vm_type, netin_bytes, netout_bytes,
                    delta_in_bytes, delta_out_bytes, boot_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (vm_id, vm_type, netin_bytes, netout_bytes,
                 delta_in, delta_out, boot_time)
            )
            if delta_in > 0 or delta_out > 0:
                conn.execute(
                    """UPDATE traffic_summary
                       SET total_in_mb = total_in_mb + ?,
                           total_out_mb = total_out_mb + ?
                       WHERE vm_id=? AND vm_type=?""",
                    (delta_in / (1024 * 1024), delta_out / (1024 * 1024),
                     vm_id, vm_type)
                )
        return delta_in, delta_out
    finally:
        conn.close()


def get_traffic_logs(vm_id=None, vm_type=None, limit=50):
    """查询流量日志"""
    conn = get_conn()
    sql = "SELECT * FROM traffic_logs WHERE 1=1"
    params = []
    if vm_id is not None:
        sql += " AND vm_id=?"
        params.append(vm_id)
    if vm_type is not None:
        sql += " AND vm_type=?"
        params.append(vm_type)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  操作日志 (action_logs) 相关操作
# ============================================================

def insert_action_log(action, target_type=None, target_id=None, detail=''):
    """插入操作日志"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO action_logs (action, target_type, target_id, detail) VALUES (?, ?, ?, ?)",
        (action, target_type, target_id, detail)
    )
    conn.commit()
    conn.close()


def get_action_logs(limit=50):
    """查询操作日志"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM action_logs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  Telegram 配置
# ============================================================

_UNSET = object()


def get_telegram_settings():
    """读取 Telegram 单例配置。"""
    conn = get_conn()
    row = conn.execute(
        """SELECT bot_token, chat_id, enabled, warning_percent, updated_at
           FROM telegram_settings WHERE id=1"""
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        'bot_token': '', 'chat_id': '', 'enabled': 0,
        'warning_percent': 80.0, 'updated_at': None,
    }


def update_telegram_settings(bot_token=_UNSET, chat_id=_UNSET,
                             enabled=_UNSET, warning_percent=_UNSET):
    """按字段更新 Telegram 配置，密钥不会写入操作日志。"""
    current = get_telegram_settings()
    updates = []
    params = []
    desired_token = current.get('bot_token', '')
    desired_chat_id = current.get('chat_id', '')
    desired_enabled = bool(current.get('enabled'))
    if bot_token is not _UNSET:
        desired_token = str(bot_token).strip()
        updates.append("bot_token=?")
        params.append(desired_token)
    if chat_id is not _UNSET:
        value = str(chat_id).strip()
        if value:
            try:
                int(value)
            except ValueError:
                return False, "会话 ID 必须是整数（群组 ID 通常为负数）"
        desired_chat_id = value
        updates.append("chat_id=?")
        params.append(value)
    if enabled is not _UNSET:
        desired_enabled = bool(enabled)
        updates.append("enabled=?")
        params.append(1 if desired_enabled else 0)
    elif desired_enabled and (not desired_token or not desired_chat_id):
        desired_enabled = False
        updates.append("enabled=0")
    if warning_percent is not _UNSET:
        try:
            value = float(warning_percent)
        except (TypeError, ValueError):
            return False, "预警比例必须是数字"
        if not math.isfinite(value) or not 1 <= value < 100:
            return False, "预警比例必须在 1（含）到 100（不含）之间"
        updates.append("warning_percent=?")
        params.append(value)
    if desired_enabled and (not desired_token or not desired_chat_id):
        return False, "启用 Telegram 推送前必须配置 Bot Token 和会话 ID"
    if not updates:
        return True, "配置未变化"

    updates.append("updated_at=datetime('now','localtime')")
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                f"UPDATE telegram_settings SET {', '.join(updates)} WHERE id=1",
                params
            )
        return True, "Telegram 配置已更新"
    finally:
        conn.close()


# ============================================================
#  LXC 网络状态检测
# ============================================================

def normalize_network_targets(targets):
    """校验并规范化分号分隔的 IPv4/IPv6 地址。"""
    if isinstance(targets, (list, tuple)):
        values = targets
    else:
        values = str(targets or '').split(';')
    normalized = []
    seen = set()
    for raw_value in values:
        value = str(raw_value).strip()
        if not value:
            continue
        try:
            value = str(ipaddress.ip_address(value))
        except ValueError:
            return False, f'无效 IP 地址: {value}'
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return True, ';'.join(normalized)


def get_network_check_settings():
    conn = get_conn()
    row = conn.execute(
        """SELECT enabled, targets, interval_hours, running,
                  last_started_at, last_completed_at, last_result, updated_at
           FROM network_check_settings WHERE id=1"""
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {
        'enabled': 0, 'targets': '', 'interval_hours': 6.0,
        'running': 0, 'last_started_at': None, 'last_completed_at': None,
        'last_result': '', 'updated_at': None,
    }


def update_network_check_settings(enabled=_UNSET, targets=_UNSET,
                                  interval_hours=_UNSET):
    current = get_network_check_settings()
    updates = []
    params = []
    desired_targets = current.get('targets', '')
    desired_enabled = bool(current.get('enabled'))
    if targets is not _UNSET:
        ok, value = normalize_network_targets(targets)
        if not ok:
            return False, value
        desired_targets = value
        updates.append('targets=?')
        params.append(value)
    if interval_hours is not _UNSET:
        try:
            value = float(interval_hours)
        except (TypeError, ValueError):
            return False, '检测周期必须是数字'
        if not math.isfinite(value) or not (1 / 60) <= value <= 168:
            return False, '检测周期必须在 1 分钟到 168 小时之间'
        updates.append('interval_hours=?')
        params.append(value)
    if enabled is not _UNSET:
        desired_enabled = bool(enabled)
        updates.append('enabled=?')
        params.append(1 if desired_enabled else 0)
    elif desired_enabled and not desired_targets:
        desired_enabled = False
        updates.append('enabled=0')
    if desired_enabled and not desired_targets:
        return False, '启用网络检测前必须至少设置一个检测 IP'
    if not updates:
        return True, '配置未变化'
    updates.append("updated_at=datetime('now','localtime')")
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                f"UPDATE network_check_settings SET {', '.join(updates)} WHERE id=1",
                params,
            )
        return True, 'LXC 网络检测配置已更新'
    finally:
        conn.close()


def try_claim_network_check(force=False, now=None):
    """原子认领一个检测周期，防止定时和手动检测重叠。"""
    now = now or datetime.datetime.now()
    conn = get_conn()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                'SELECT * FROM network_check_settings WHERE id=1'
            ).fetchone()
            settings = dict(row)
            if not force and not settings['enabled']:
                return False, '网络检测未启用'
            ok, normalized = normalize_network_targets(settings['targets'])
            if not ok or not normalized:
                return False, '未配置有效检测 IP'

            last_started = None
            if settings.get('last_started_at'):
                try:
                    last_started = datetime.datetime.strptime(
                        settings['last_started_at'], '%Y-%m-%d %H:%M:%S'
                    )
                except (TypeError, ValueError):
                    last_started = None
            if settings['running']:
                # 服务异常退出后允许 24 小时以上的陈旧锁自动恢复。
                if last_started and (now - last_started).total_seconds() < 24 * 3600:
                    return False, '已有网络检测正在运行'
            if not force and last_started:
                interval = float(settings['interval_hours']) * 3600
                if (now - last_started).total_seconds() < interval:
                    return False, '尚未到达下次检测时间'

            started = now.strftime('%Y-%m-%d %H:%M:%S')
            conn.execute(
                """UPDATE network_check_settings
                   SET running=1, last_started_at=?, last_result='检测中'
                   WHERE id=1""",
                (started,),
            )
            settings['targets'] = normalized
            settings['target_list'] = normalized.split(';')
            settings['last_started_at'] = started
            settings['running'] = 1
            return True, settings
    finally:
        conn.close()


def finish_network_check(result):
    conn = get_conn()
    with conn:
        conn.execute(
            """UPDATE network_check_settings
               SET running=0, last_completed_at=datetime('now','localtime'),
                   last_result=? WHERE id=1""",
            (str(result)[:500],),
        )
    conn.close()


def recover_interrupted_network_check():
    """Bot 重启时释放上次未完成的检测，使其能在启动后重新运行。"""
    conn = get_conn()
    with conn:
        cursor = conn.execute(
            """UPDATE network_check_settings
               SET running=0, last_started_at=NULL,
                   last_result='上次检测被服务重启中断，等待重新检测'
               WHERE id=1 AND running=1"""
        )
    conn.close()
    return cursor.rowcount == 1


def record_network_check(vm_id, vm_name, target, success, detail=''):
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO network_check_logs
               (vm_id, vm_name, target, success, detail)
               VALUES (?, ?, ?, ?, ?)""",
            (vm_id, vm_name or '', target, 1 if success else 0, str(detail)[:1000]),
        )
        conn.execute(
            """DELETE FROM network_check_logs
               WHERE id NOT IN (
                   SELECT id FROM network_check_logs ORDER BY id DESC LIMIT 1000
               )"""
        )
    conn.close()


def get_network_check_logs(limit=20, failures_only=False):
    conn = get_conn()
    sql = 'SELECT * FROM network_check_logs'
    if failures_only:
        sql += ' WHERE success=0'
    sql += ' ORDER BY id DESC LIMIT ?'
    rows = conn.execute(sql, (max(1, int(limit)),)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_usage_alerts(minimum_percent=80):
    """返回达到指定用量百分比的 VM/组记录。"""
    minimum_percent = max(0, float(minimum_percent))
    conn = get_conn()
    rows = conn.execute(
        """SELECT ts.*, gv.vm_name, g.name AS group_name,
                  g.traffic_limit_mb,
                  CASE WHEN g.traffic_limit_mb > 0 THEN
                      ((ts.total_in_mb + ts.total_out_mb) * 100.0 / g.traffic_limit_mb)
                  ELSE 0 END AS usage_percent
           FROM traffic_summary ts
           JOIN group_vms gv
             ON gv.group_id=ts.group_id AND gv.vm_id=ts.vm_id
            AND gv.vm_type=ts.vm_type
           JOIN groups g ON g.id=ts.group_id
           WHERE g.traffic_limit_mb > 0
             AND ((ts.total_in_mb + ts.total_out_mb) * 100.0 / g.traffic_limit_mb) >= ?
           ORDER BY usage_percent DESC, ts.vm_id""",
        (minimum_percent,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_vm_traffic_details(vm_id, vm_type=None):
    """返回某个 VM 在各组中的流量明细。"""
    conn = get_conn()
    sql = """SELECT ts.*, gv.vm_name, g.name AS group_name,
                    g.traffic_limit_mb
             FROM traffic_summary ts
             JOIN group_vms gv
               ON gv.group_id=ts.group_id AND gv.vm_id=ts.vm_id
              AND gv.vm_type=ts.vm_type
             JOIN groups g ON g.id=ts.group_id
             WHERE ts.vm_id=?"""
    params = [vm_id]
    if vm_type is not None:
        sql += " AND ts.vm_type=?"
        params.append(vm_type)
    sql += " ORDER BY ts.vm_type, ts.group_id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def claim_traffic_notification(vm_id, vm_type, group_id, notification_type):
    """原子认领一次通知；同一流量周期内仅一个进程能认领成功。"""
    column = {
        'warning': 'warning_sent',
        'shutdown': 'shutdown_notified',
    }.get(notification_type)
    if column is None:
        raise ValueError("不支持的通知类型")
    conn = get_conn()
    try:
        with conn:
            cursor = conn.execute(
                f"""UPDATE traffic_summary SET {column}=1
                    WHERE vm_id=? AND vm_type=? AND group_id=? AND {column}=0""",
                (vm_id, vm_type, group_id)
            )
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_traffic_notification(vm_id, vm_type, group_id, notification_type):
    """发送失败时释放通知认领，以便下次监控重试。"""
    column = {
        'warning': 'warning_sent',
        'shutdown': 'shutdown_notified',
    }.get(notification_type)
    if column is None:
        raise ValueError("不支持的通知类型")
    conn = get_conn()
    with conn:
        conn.execute(
            f"""UPDATE traffic_summary SET {column}=0
                WHERE vm_id=? AND vm_type=? AND group_id=?""",
            (vm_id, vm_type, group_id)
        )
    conn.close()


def claim_group_all_shutdown_notification(group_id):
    """原子认领一次全组关机通知。"""
    conn = get_conn()
    try:
        with conn:
            cursor = conn.execute(
                """UPDATE groups SET all_shutdown_notified=1
                   WHERE id=? AND all_shutdown_notified=0""",
                (group_id,)
            )
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_group_all_shutdown_notification(group_id):
    """全组关机消息发送失败时释放认领，供下一轮重试。"""
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE groups SET all_shutdown_notified=0 WHERE id=?", (group_id,)
        )
    conn.close()


def update_vm_name(vm_id, vm_type, vm_name):
    """刷新 VM 在所有组中的显示名称；空查询结果不覆盖已有名称。"""
    vm_name = str(vm_name or '').strip()
    if not vm_name:
        return False
    conn = get_conn()
    with conn:
        cursor = conn.execute(
            """UPDATE group_vms SET vm_name=?
               WHERE vm_id=? AND vm_type=?""",
            (vm_name, vm_id, vm_type)
        )
    conn.close()
    return cursor.rowcount > 0


# ============================================================
#  超限关机状态
# ============================================================

def record_shutdown_request(vm_id, vm_type, boot_time_at_request=None, group_id=None):
    """记录已被 PVE 接受的超限关机请求。"""
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO shutdown_state
               (vm_id, vm_type, requested_at, boot_time_at_request, group_id, stopped_seen)
               VALUES (?, ?, datetime('now','localtime'), ?, ?, 0)
               ON CONFLICT(vm_id, vm_type) DO UPDATE SET
                   requested_at=excluded.requested_at,
                   boot_time_at_request=excluded.boot_time_at_request,
                   group_id=excluded.group_id,
                   stopped_seen=0""",
            (vm_id, vm_type, boot_time_at_request, group_id)
        )
    conn.close()


def record_shutdown_success(vm_id, vm_type, detail, boot_time_at_request=None,
                            group_id=None):
    """原子地记录关机日志和待确认重启状态。"""
    conn = get_conn()
    try:
        with conn:
            conn.execute(
                """INSERT INTO action_logs
                   (action, target_type, target_id, detail)
                   VALUES ('shutdown', 'vm', ?, ?)""",
                (vm_id, detail)
            )
            conn.execute(
                """INSERT INTO shutdown_state
                   (vm_id, vm_type, requested_at, boot_time_at_request, group_id, stopped_seen)
                   VALUES (?, ?, datetime('now','localtime'), ?, ?, 0)
                   ON CONFLICT(vm_id, vm_type) DO UPDATE SET
                       requested_at=excluded.requested_at,
                       boot_time_at_request=excluded.boot_time_at_request,
                       group_id=excluded.group_id,
                       stopped_seen=0""",
                (vm_id, vm_type, boot_time_at_request, group_id)
            )
    finally:
        conn.close()


def get_shutdown_state(vm_id, vm_type):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM shutdown_state WHERE vm_id=? AND vm_type=?",
        (vm_id, vm_type)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_shutdown_stopped(vm_id, vm_type):
    conn = get_conn()
    with conn:
        conn.execute(
            """UPDATE shutdown_state SET stopped_seen=1
               WHERE vm_id=? AND vm_type=?""",
            (vm_id, vm_type)
        )
    conn.close()


def clear_shutdown_state(vm_id, vm_type):
    conn = get_conn()
    with conn:
        conn.execute(
            "DELETE FROM shutdown_state WHERE vm_id=? AND vm_type=?",
            (vm_id, vm_type)
        )
    conn.close()


def auto_reset_vm_traffic(vm_id, vm_type, detail=''):
    """原子地重置 VM 在全部组中的流量并完成关机状态。"""
    conn = get_conn()
    try:
        with conn:
            groups = conn.execute(
                """SELECT gv.group_id, g.name AS group_name
                   FROM group_vms gv JOIN groups g ON g.id=gv.group_id
                   WHERE gv.vm_id=? AND gv.vm_type=?""",
                (vm_id, vm_type)
            ).fetchall()
            now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for group in groups:
                conn.execute(
                    """INSERT INTO traffic_resets
                       (group_id, vm_id, vm_type, reason)
                       VALUES (?, ?, ?, 'auto_restart')""",
                    (group['group_id'], vm_id, vm_type)
                )
                conn.execute(
                    "UPDATE groups SET all_shutdown_notified=0 WHERE id=?",
                    (group['group_id'],)
                )
            conn.execute(
                """UPDATE traffic_summary
                   SET total_in_mb=0, total_out_mb=0, last_reset=?,
                       warning_sent=0, shutdown_notified=0
                   WHERE vm_id=? AND vm_type=?""",
                (now, vm_id, vm_type)
            )
            conn.execute(
                "DELETE FROM shutdown_state WHERE vm_id=? AND vm_type=?",
                (vm_id, vm_type)
            )
            conn.execute(
                """INSERT INTO action_logs
                   (action, target_type, target_id, detail)
                   VALUES ('reset', 'vm', ?, ?)""",
                (vm_id, detail)
            )
        return [dict(group) for group in groups]
    finally:
        conn.close()


def reset_vm_traffic_all_groups(vm_id, vm_type, detail='Telegram 手动重置'):
    """手动重置一台 VM 在全部组中的用量，不影响任何其他 VM。"""
    conn = get_conn()
    try:
        with conn:
            groups = conn.execute(
                """SELECT gv.group_id, g.name AS group_name
                   FROM group_vms gv JOIN groups g ON g.id=gv.group_id
                   WHERE gv.vm_id=? AND gv.vm_type=?""",
                (vm_id, vm_type)
            ).fetchall()
            if not groups:
                return []
            for group in groups:
                conn.execute(
                    """INSERT INTO traffic_resets
                       (group_id, vm_id, vm_type, reason)
                       VALUES (?, ?, ?, 'telegram_manual')""",
                    (group['group_id'], vm_id, vm_type)
                )
                conn.execute(
                    "UPDATE groups SET all_shutdown_notified=0 WHERE id=?",
                    (group['group_id'],)
                )
            conn.execute(
                """UPDATE traffic_summary
                   SET total_in_mb=0, total_out_mb=0,
                       last_reset=datetime('now','localtime'),
                       warning_sent=0, shutdown_notified=0
                   WHERE vm_id=? AND vm_type=?""",
                (vm_id, vm_type)
            )
            conn.execute(
                "DELETE FROM shutdown_state WHERE vm_id=? AND vm_type=?",
                (vm_id, vm_type)
            )
            conn.execute(
                """INSERT INTO action_logs
                   (action, target_type, target_id, detail)
                   VALUES ('reset', 'vm', ?, ?)""",
                (vm_id, detail)
            )
        return [dict(group) for group in groups]
    finally:
        conn.close()
