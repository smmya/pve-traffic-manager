# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - 数据库操作层
"""

import sqlite3
import os
from config import DB_PATH, DATA_DIR


def get_conn():
    """获取数据库连接"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
    ''')

    conn.commit()
    conn.close()


# ============================================================
#  组 (Groups) 相关操作
# ============================================================

def create_group(name, traffic_limit_mb, notify_cmd=''):
    """创建管理组"""
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
        "SELECT id, name, traffic_limit_mb, notify_cmd, created_at FROM groups ORDER BY id"
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

    try:
        conn.execute(
            "UPDATE groups SET name=?, traffic_limit_mb=?, notify_cmd=? WHERE id=?",
            (new_name, new_limit, new_notify, group_id)
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
    conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    conn.commit()
    conn.close()
    return True


# ============================================================
#  组内虚拟机 (group_vms) 相关操作
# ============================================================

def add_vm_to_group(group_id, vm_id, vm_type, vm_name=''):
    """将虚拟机加入组"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO group_vms (group_id, vm_id, vm_type, vm_name) VALUES (?, ?, ?, ?)",
            (group_id, vm_id, vm_type, vm_name)
        )
        # 同时创建 traffic_summary 记录
        conn.execute(
            "INSERT OR IGNORE INTO traffic_summary (vm_id, vm_type, group_id) VALUES (?, ?, ?)",
            (vm_id, vm_type, group_id)
        )
        conn.commit()
        return True, "加入成功"
    except sqlite3.IntegrityError:
        return False, "该虚拟机已在此组中"
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
        """SELECT DISTINCT gv.vm_id, gv.vm_type, gv.vm_name,
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
        "UPDATE traffic_summary SET total_in_mb=0, total_out_mb=0, last_reset=? WHERE group_id=?",
        (now, group_id)
    )
    conn.commit()
    conn.close()
    return True


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

def insert_traffic_log(vm_id, vm_type, netin_bytes, netout_bytes, delta_in_bytes, delta_out_bytes):
    """插入流量日志"""
    conn = get_conn()
    conn.execute(
        """INSERT INTO traffic_logs (vm_id, vm_type, netin_bytes, netout_bytes, delta_in_bytes, delta_out_bytes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (vm_id, vm_type, netin_bytes, netout_bytes, delta_in_bytes, delta_out_bytes)
    )
    conn.commit()
    conn.close()


def get_last_traffic_log(vm_id, vm_type):
    """获取虚拟机最后一次流量记录（用于计算 delta）"""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM traffic_logs
           WHERE vm_id=? AND vm_type=?
           ORDER BY timestamp DESC LIMIT 1""",
        (vm_id, vm_type)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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
    sql += " ORDER BY timestamp DESC LIMIT ?"
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
        "SELECT * FROM action_logs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
