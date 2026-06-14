#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - 后台监控脚本
由 crontab 定时调用，实现流量采集、超限判断、通知和关机

用法:
    python monitor.py               # 执行一次监控循环 (采集 + 超限检查)
    python monitor.py --dry-run     # 演习模式，不实际关机
    python monitor.py --collect-only # 仅采集流量，跳过超限检查
"""

import sys
import subprocess
import datetime
import db
import pve
from config import DB_PATH


def log(msg):
    """输出带时间戳的日志"""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] {msg}")


def execute_notify(notify_cmd, vm_id, vm_name, group_name, usage_mb, limit_mb, vm_type):
    """执行通知命令"""
    if not notify_cmd:
        return True, "未配置通知命令"

    # 构建命令（替换变量为参数形式）
    cmd = notify_cmd
    # 如果命令中包含变量占位符，进行替换
    cmd = cmd.replace('{vm_id}', str(vm_id))
    cmd = cmd.replace('{vm_name}', vm_name)
    cmd = cmd.replace('{group}', group_name)
    cmd = cmd.replace('{usage}', f"{usage_mb:.2f}")
    cmd = cmd.replace('{limit}', f"{limit_mb:.2f}")
    cmd = cmd.replace('{vm_type}', vm_type)

    try:
        # 使用 shell=True 以支持管道等复杂命令
        result = subprocess.run(
            cmd, shell=True,
            capture_output=True, text=True,
            timeout=30
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "通知命令执行超时"
    except Exception as e:
        return False, str(e)


def collect_traffic(vm_id, vm_type):
    """
    采集单台虚拟机的流量
    返回: (delta_in_bytes, delta_out_bytes) 或 (0, 0) 首次采集
    """
    netin, netout, status = pve.get_vm_network_traffic(vm_id, vm_type)

    if status != 'running':
        return 0, 0

    last_log = db.get_last_traffic_log(vm_id, vm_type)

    if last_log is None:
        # 首次采集，记录基线但不计增量
        db.insert_traffic_log(vm_id, vm_type, netin, netout, 0, 0)
        return 0, 0

    # 处理计数器归零（VM 重启导致 netin/netout 归零）
    if netin < last_log['netin_bytes'] or netout < last_log['netout_bytes']:
        # 计数器归零，本周期增量 = 当前值（因为是从0开始的）
        delta_in = netin
        delta_out = netout
    else:
        delta_in = netin - last_log['netin_bytes']
        delta_out = netout - last_log['netout_bytes']

    # 写入日志
    db.insert_traffic_log(vm_id, vm_type, netin, netout, delta_in, delta_out)

    return delta_in, delta_out


def check_and_shutdown(vm_id, vm_type, vm_name, group_id, group_name, limit_mb, dry_run=False):
    """
    检查单个 VM 是否超限，超限则关机
    返回: (action_taken, detail)
    """
    summary = db.get_vm_traffic_summary(vm_id, vm_type, group_id)
    if not summary:
        return False, "无流量数据"

    total_mb = summary['total_in_mb'] + summary['total_out_mb']

    if total_mb < limit_mb:
        return False, f"未超限 ({total_mb:.2f}/{limit_mb:.2f} MB)"

    # === 超限处理 ===
    type_label = 'KVM' if vm_type == 'qemu' else 'LXC'

    # 1. 执行通知
    group = db.get_group_by_id(group_id)
    notify_cmd = group.get('notify_cmd', '') if group else ''
    if notify_cmd:
        notify_ok, notify_result = execute_notify(
            notify_cmd, vm_id, vm_name, group_name, total_mb, limit_mb, vm_type
        )
        if not notify_ok:
            log(f"  [通知失败] {type_label} {vm_id}: {notify_result}")

    # 2. 关机
    if dry_run:
        log(f"  [演习] 将关闭 {type_label} {vm_id} '{vm_name}' (流量: {total_mb:.2f}/{limit_mb:.2f} MB)")
        return True, f"演习模式 - 将关闭 {type_label} {vm_id} '{vm_name}'"
    else:
        ok, msg = pve.shutdown_vm(vm_id, vm_type)
        if ok:
            log(f"  [已关机] {type_label} {vm_id} '{vm_name}' (流量: {total_mb:.2f}/{limit_mb:.2f} MB)")
            # 仅在成功关机后记录日志
            detail = f"{type_label} {vm_id} '{vm_name}' 超限关机: {total_mb:.2f}/{limit_mb:.2f} MB (组: {group_name})"
            db.insert_action_log('shutdown', 'vm', vm_id, detail)
        else:
            log(f"  [关机失败] {type_label} {vm_id} '{vm_name}': {msg}")
        return ok, f"{'成功' if ok else '失败'}: {msg}"


def check_auto_reset(vm_id, vm_type):
    """
    检查VM是否被PTM超限关机后重新启动，如果是则自动重置流量
    返回: True 如果执行了重置
    """
    # 1. 查最近一次关机记录
    shutdown_log = db.get_last_shutdown_for_vm(vm_id)
    if not shutdown_log:
        return False

    detail = shutdown_log.get('detail', '')
    if '超限关机' not in detail:
        return False  # 不是PTM关的，不重置

    # 2. 检查是否已经处理过这次关机事件（防止重复重置）
    last_reset = db.get_last_auto_reset_for_vm(vm_id)
    if last_reset and last_reset.get('reset_at', '') >= shutdown_log.get('created_at', ''):
        return False  # 已经重置过了，跳过

    # 3. 检查VM当前是否在运行（说明已被手动重启）
    status = pve.get_vm_status(vm_id, vm_type)
    if not status or status.get('status') != 'running':
        return False  # 还没启动，不重置

    # 4. 自动重置该VM在每个组中的流量
    type_label = 'KVM' if vm_type == 'qemu' else 'LXC'
    groups = db.get_vm_groups(vm_id, vm_type)
    for g in groups:
        db.reset_vm_traffic(vm_id, vm_type, g['group_id'])
        log(f"  [自动重置] {type_label} {vm_id} 检测到PTM超限关机后重启，已重置流量 (组: {g['group_name']})")

    # 记录操作日志
    db.insert_action_log('reset', 'vm', vm_id,
                         f"自动重置: {type_label} {vm_id} 超限关机后重启")

    return True


def run_monitor(dry_run=False, collect_only=False):
    """执行一次完整的监控循环"""
    if collect_only:
        mode = "仅采集模式"
    elif dry_run:
        mode = "演习模式"
    else:
        mode = "监控模式"
    log(f"=== PVE 流量监控开始 ({mode}) ===")

    # 获取所有已管理的虚拟机
    managed = db.get_all_managed_vms()
    if not managed:
        log("无已管理虚拟机，跳过")
        return

    log(f"管理 {len(managed)} 台虚拟机")

    # --- 自动重置检查 ---
    for vm in managed:
        check_auto_reset(vm['vm_id'], vm['vm_type'])

    # --- 采集流量 ---
    for vm in managed:
        vm_id = vm['vm_id']
        vm_type = vm['vm_type']
        vm_name = vm['vm_name']
        type_label = 'KVM' if vm_type == 'qemu' else 'LXC'

        delta_in, delta_out = collect_traffic(vm_id, vm_type)
        log(f"  {type_label} {vm_id} '{vm_name}': +{delta_in / 1024:.1f} KB in / +{delta_out / 1024:.1f} KB out")

        # --- 更新各组流量汇总 ---
        vm_groups = db.get_vm_groups(vm_id, vm_type)
        for vg in vm_groups:
            if delta_in > 0 or delta_out > 0:
                db.update_traffic_summary(vm_id, vm_type, vg['group_id'], delta_in, delta_out)

    # --- 超限检查 (仅采集模式跳过) ---
    if collect_only:
        log("仅采集模式，跳过超限检查")
    else:
        log("--- 超限检查 ---")
        for vm in managed:
            vm_id = vm['vm_id']
            vm_type = vm['vm_type']
            vm_name = vm['vm_name']

            vm_groups = db.get_vm_groups(vm_id, vm_type)
            for vg in vm_groups:
                action, detail = check_and_shutdown(
                    vm_id, vm_type, vm_name,
                    vg['group_id'], vg['group_name'], vg['traffic_limit_mb'],
                    dry_run
                )
                if action:
                    log(f"  [超限] 组 '{vg['group_name']}' :: {detail}")

    log("=== PVE 流量监控结束 ===")


if __name__ == '__main__':
    db.init_db()

    dry_run = '--dry-run' in sys.argv
    collect_only = '--collect-only' in sys.argv

    try:
        run_monitor(dry_run, collect_only)
    except Exception as e:
        log(f"监控异常: {e}")
        import traceback
        traceback.print_exc()
