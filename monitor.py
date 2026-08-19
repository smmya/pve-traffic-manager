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
import shlex
import subprocess
import datetime
import db
import pve

try:
    import telegram_service
except ImportError:  # 兼容从旧版升级但尚未补齐 Telegram 文件的节点
    telegram_service = None


SHUTDOWN_RETRY_SECONDS = 15 * 60


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
    cmd = cmd.replace('{vm_name}', shlex.quote(str(vm_name)))
    cmd = cmd.replace('{group}', shlex.quote(str(group_name)))
    cmd = cmd.replace('{usage}', f"{usage_mb:.2f}")
    cmd = cmd.replace('{limit}', f"{limit_mb:.2f}")
    cmd = cmd.replace('{vm_type}', shlex.quote(str(vm_type)))

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
    返回: (delta_in_bytes, delta_out_bytes) 首次/正常采集
           None 表示 VM 不在运行状态，跳过
    """
    netin, netout, status, boot_time = pve.get_vm_network_snapshot(vm_id, vm_type)

    if status != 'running':
        return None

    # 采样日志和全部组汇总必须在同一事务内更新，避免中途失败后永久漏计。
    return db.record_traffic_sample(
        vm_id, vm_type, netin, netout, boot_time=boot_time
    )


def _send_telegram_usage_notification(notification_type, vm_id, vm_type,
                                      vm_name, group_id, group_name,
                                      usage_mb, limit_mb):
    """按 VM/组/流量周期去重发送 Telegram 预警或关机通知。"""
    if telegram_service is None or not telegram_service.notifications_ready():
        return False
    if not db.claim_traffic_notification(
        vm_id, vm_type, group_id, notification_type
    ):
        return False

    percent = usage_mb * 100 / limit_mb if limit_mb > 0 else 0
    type_label = 'KVM' if vm_type == 'qemu' else 'LXC'
    if notification_type == 'warning':
        title = '⚠️ PTM 流量预警'
        ending = '请及时检查流量使用情况。'
        action_name = 'telegram_warning'
    else:
        title = '🛑 PTM 超限关机通知'
        ending = 'PTM 已向 PVE 提交关机请求；用户重启后仅重置该机器的流量。'
        action_name = 'telegram_shutdown'
    text = (
        f'{title}\n'
        f"虚拟机：{type_label} {vm_id} '{vm_name or '-'}'\n"
        f'管理组：{group_name}\n'
        f'已用流量：{usage_mb:.2f} MB / {limit_mb:.2f} MB ({percent:.1f}%)\n'
        f'{ending}'
    )
    ok, detail = telegram_service.send_message(text)
    if not ok:
        db.release_traffic_notification(
            vm_id, vm_type, group_id, notification_type
        )
        log(f'  [Telegram 发送失败] {type_label} {vm_id}: {detail}')
        return False
    db.insert_action_log(
        action_name, target_type='vm', target_id=vm_id,
        detail=f'{group_name}: {usage_mb:.2f}/{limit_mb:.2f} MB ({percent:.1f}%)'
    )
    return True


def check_usage_warning(vm_id, vm_type, vm_name, group_id, group_name,
                        limit_mb, dry_run=False):
    """达到预警比例后发送一次 Telegram 通知。"""
    if dry_run or telegram_service is None or not telegram_service.notifications_ready():
        return False
    summary = db.get_vm_traffic_summary(vm_id, vm_type, group_id)
    if not summary or limit_mb <= 0:
        return False
    total_mb = summary['total_in_mb'] + summary['total_out_mb']
    percent = float(db.get_telegram_settings().get('warning_percent', 80))
    if total_mb < limit_mb * percent / 100:
        return False
    return _send_telegram_usage_notification(
        'warning', vm_id, vm_type, vm_name, group_id, group_name,
        total_mb, limit_mb,
    )


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

    shutdown_state = db.get_shutdown_state(vm_id, vm_type)
    if shutdown_state:
        if not dry_run and (
            shutdown_state.get('group_id') is None
            or shutdown_state.get('group_id') == group_id
        ):
            _send_telegram_usage_notification(
                'shutdown', vm_id, vm_type, vm_name, group_id, group_name,
                total_mb, limit_mb,
            )
        return False, "已有待完成的关机请求"

    # === 超限处理 ===
    type_label = 'KVM' if vm_type == 'qemu' else 'LXC'

    # 0. 检查VM是否在运行（已关机的跳过）
    vm_status = pve.get_vm_status(vm_id, vm_type)
    if not vm_status or vm_status.get('status') != 'running':
        return False, "VM不在运行状态，无需关机"

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
            detail = f"{type_label} {vm_id} '{vm_name}' 超限关机: {total_mb:.2f}/{limit_mb:.2f} MB (组: {group_name})"
            db.record_shutdown_success(
                vm_id, vm_type, detail, pve.get_vm_boot_time(vm_status), group_id
            )
            _send_telegram_usage_notification(
                'shutdown', vm_id, vm_type, vm_name, group_id, group_name,
                total_mb, limit_mb,
            )
        else:
            log(f"  [关机失败] {type_label} {vm_id} '{vm_name}': {msg}")
        # 已执行过通知和关机尝试；同一轮不应因 VM 属于多个组而重复执行。
        return True, f"{'成功' if ok else '失败'}: {msg}"


def check_auto_reset(vm_id, vm_type):
    """
    检查VM是否被PTM超限关机后重新启动，如果是则自动重置流量
    返回: True 如果执行了重置
    """
    state = db.get_shutdown_state(vm_id, vm_type)
    if not state:
        return False

    # 必须先观察到 stopped，或确认启动时间已变化，才能认定发生了重启。
    # qm/pct shutdown 返回成功时 VM 往往仍短暂处于 running，不能立即清零。
    status = pve.get_vm_status(vm_id, vm_type)
    if not status:
        return False
    if status.get('status') != 'running':
        if status.get('status') == 'stopped' and not state['stopped_seen']:
            db.mark_shutdown_stopped(vm_id, vm_type)
        return False

    current_boot_time = pve.get_vm_boot_time(status)
    requested_boot_time = state.get('boot_time_at_request')
    boot_changed = (
        current_boot_time is not None
        and requested_boot_time is not None
        and abs(current_boot_time - requested_boot_time) > db.BOOT_TIME_TOLERANCE_SECONDS
    )
    if not state['stopped_seen'] and not boot_changed:
        try:
            requested_at = datetime.datetime.strptime(
                state['requested_at'], '%Y-%m-%d %H:%M:%S'
            )
            age = (datetime.datetime.now() - requested_at).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age >= SHUTDOWN_RETRY_SECONDS:
            db.clear_shutdown_state(vm_id, vm_type)
            log(f"  [警告] {vm_type.upper()} {vm_id} 关机请求超时，将允许下轮重试")
        return False

    type_label = 'KVM' if vm_type == 'qemu' else 'LXC'
    detail = f"自动重置: {type_label} {vm_id} 超限关机后重启"
    groups = db.auto_reset_vm_traffic(vm_id, vm_type, detail)
    for g in groups:
        log(f"  [自动重置] {type_label} {vm_id} 检测到PTM超限关机后重启，已重置流量 (组: {g['group_name']})")

    return True


def _acquire_monitor_lock():
    """非阻塞获取进程锁；返回文件句柄，None 表示已有监控在运行。"""
    return db.acquire_monitor_lock()


def run_monitor(dry_run=False, collect_only=False):
    """带单实例锁执行监控，避免重叠任务重复计费或关机。"""
    lock_handle = _acquire_monitor_lock()
    if lock_handle is None:
        log("已有监控任务正在运行，本次跳过")
        return False
    try:
        _run_monitor(dry_run, collect_only)
        return True
    finally:
        db.release_monitor_lock(lock_handle)


def _run_monitor(dry_run=False, collect_only=False):
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

    # 演习/仅采集模式不执行任何自动重置副作用。
    if not dry_run and not collect_only:
        for vm in managed:
            check_auto_reset(vm['vm_id'], vm['vm_type'])

    # --- 采集流量 ---
    for vm in managed:
        vm_id = vm['vm_id']
        vm_type = vm['vm_type']
        vm_name = vm['vm_name']
        type_label = 'KVM' if vm_type == 'qemu' else 'LXC'

        result = collect_traffic(vm_id, vm_type)
        if result is None:
            continue  # 不在运行，跳过
        delta_in, delta_out = result

        log(f"  {type_label} {vm_id} '{vm_name}': +{delta_in / 1024:.1f} KB in / +{delta_out / 1024:.1f} KB out")

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
                check_usage_warning(
                    vm_id, vm_type, vm_name,
                    vg['group_id'], vg['group_name'], vg['traffic_limit_mb'],
                    dry_run,
                )
                action, detail = check_and_shutdown(
                    vm_id, vm_type, vm_name,
                    vg['group_id'], vg['group_name'], vg['traffic_limit_mb'],
                    dry_run
                )
                if action:
                    log(f"  [超限] 组 '{vg['group_name']}' :: {detail}")
                    break

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
        sys.exit(1)
