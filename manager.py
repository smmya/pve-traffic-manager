# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - CLI 前台交互主程序
用法: python manager.py
"""

import os
import sys
import re
import math
import shlex
import subprocess
import getpass
import db
import pve
from config import BASE_DIR, PYTHON_PATH, MONITOR_SCRIPT, DEFAULT_MONITOR_INTERVAL

try:
    import telegram_service
except ImportError:  # 兼容旧升级器第一次未拉取新增文件的情况
    telegram_service = None

CRONTAB_MARKER = "# pve-traffic-manager monitor"


# ============================================================
#  工具函数
# ============================================================

def clear_screen():
    """清屏"""
    os.system('cls' if os.name == 'nt' else 'clear')


def print_header(title):
    """打印标题栏"""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_table(headers, rows, col_widths=None):
    """打印对齐表格"""
    if not rows:
        print("  (无数据)")
        return

    if col_widths is None:
        col_widths = [max(len(str(h)), 8) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    # 表头分隔线
    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"

    # 打印表头
    print(sep)
    header_line = "| " + " | ".join(
        str(h).ljust(col_widths[i]) for i, h in enumerate(headers)
    ) + " |"
    print(header_line)
    print(sep)

    # 打印数据行
    for row in rows:
        line = "| " + " | ".join(
            str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)
        ) + " |"
        print(line)

    print(sep)


def format_bytes(size_bytes):
    """字节转可读格式"""
    if size_bytes is None:
        return "N/A"
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes} B"


def format_gb(mb):
    """MB 转 GB 可读格式（始终以 GB 显示）"""
    if mb is None:
        return "N/A"
    return f"{mb / 1024:.2f} GB"


def input_choice(prompt, valid_choices):
    """获取用户选择，带验证"""
    while True:
        try:
            choice = input(prompt).strip()
            if choice in valid_choices:
                return choice
            print(f"  无效选择，请输入: {', '.join(valid_choices)}")
        except (EOFError, KeyboardInterrupt):
            print()
            return '0'


def input_number(prompt, allow_empty=False):
    """获取数字输入"""
    while True:
        try:
            val = input(prompt).strip()
            if allow_empty and val == '':
                return None
            number = float(val)
            if not math.isfinite(number):
                raise ValueError
            return number
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        except ValueError:
            print("  请输入有效数字")


def input_integer(prompt, allow_empty=False):
    """获取整数输入，拒绝小数、NaN 和无穷值。"""
    while True:
        try:
            val = input(prompt).strip()
            if allow_empty and val == '':
                return None
            return int(val)
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        except ValueError:
            print("  请输入有效整数")


def input_vm_range(prompt, available_vms=None):
    """
    解析虚拟机范围输入
    支持格式: 100, 100-110, 100,102,105-110
    返回: [(vmid, vm_type), ...] 或空列表
    """
    raw = input(prompt).strip()
    if not raw:
        return []

    # 获取所有可用虚拟机用于匹配
    all_vms = pve.get_all_vms() if available_vms is None else available_vms
    vm_map = {}  # key: str(vmid), value: list of (vmid, vm_type)
    for vm in all_vms:
        vmid = vm.get('vmid', vm.get('vm_id'))
        vm_type = vm.get('type', vm.get('vm_type'))
        if vmid is None or vm_type not in ('qemu', 'lxc'):
            continue
        key = str(vmid)
        if key not in vm_map:
            vm_map[key] = []
        vm_map[key].append((vmid, vm_type))

    result = []
    parts = re.split(r'[,，]', raw)
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # 范围写法: 100-110
        range_match = re.match(r'^(\d+)\s*[-~]\s*(\d+)$', part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start > end:
                start, end = end, start
            available_ids = sorted(
                int(key) for key in vm_map
                if start <= int(key) <= end
            )
            for vmid in available_ids:
                key = str(vmid)
                for v in vm_map[key]:
                    if v not in result:
                        result.append(v)
            missing_count = end - start + 1 - len(available_ids)
            if missing_count:
                if missing_count <= 20:
                    available_set = set(available_ids)
                    for vmid in range(start, end + 1):
                        if vmid not in available_set:
                            print(f"  [警告] VMID {vmid} 不存在，已跳过")
                else:
                    print(f"  [警告] 范围内有 {missing_count} 个 VMID 不存在，已跳过")
        else:
            # 单个 VMID
            try:
                vmid = int(part)
                key = str(vmid)
                if key in vm_map:
                    for v in vm_map[key]:
                        if v not in result:
                            result.append(v)
                else:
                    print(f"  [警告] VMID {vmid} 不存在，已跳过")
            except ValueError:
                print(f"  [警告] 无效输入: {part}，已跳过")

    return result


def confirm_action(prompt):
    """确认操作"""
    return input(f"{prompt} (y/N): ").strip().lower() == 'y'


# ============================================================
#  组管理菜单
# ============================================================

def menu_group_management():
    """组管理子菜单"""
    while True:
        clear_screen()
        print_header("组管理")

        groups = db.get_all_groups()
        if groups:
            headers = ['ID', '组名', '流量限额', '通知命令', '创建时间']
            rows = []
            for g in groups:
                notify_info = g['notify_cmd'][:30] + '...' if len(g.get('notify_cmd', '')) > 30 else g.get('notify_cmd', '-')
                rows.append([
                    str(g['id']),
                    g['name'],
                    format_gb(g['traffic_limit_mb']),
                    notify_info,
                    g['created_at']
                ])
            print_table(headers, rows)
        else:
            print("\n  暂无管理组，请先创建\n")

        print()
        print("  1. 创建新组")
        print("  2. 修改组")
        print("  3. 删除组")
        print("  4. 查看组内虚拟机")
        print("  0. 返回主菜单")

        choice = input_choice("  请选择 [0-4]: ", ['0', '1', '2', '3', '4'])

        if choice == '0':
            break
        elif choice == '1':
            _create_group()
        elif choice == '2':
            _modify_group()
        elif choice == '3':
            _delete_group()
        elif choice == '4':
            _view_group_vms()


def _create_group():
    """创建新组"""
    clear_screen()
    print_header("创建新组")

    name = input("  组名称: ").strip()
    if not name:
        print("  组名不能为空")
        input("  按回车返回...")
        return

    existing = db.get_group_by_name(name)
    if existing:
        print(f"  组 '{name}' 已存在")
        input("  按回车返回...")
        return

    limit = input_number("  流量限额 (GB): ")
    if limit is None or limit <= 0:
        print("  限额必须为正数")
        input("  按回车返回...")
        return
    limit = limit * 1024  # 转换为 MB 存储

    notify = input("  通知命令 (可选，直接回车跳过): ").strip()

    success, result = db.create_group(name, limit, notify)
    if success:
        print(f"\n  [成功] 组 '{name}' 创建成功 (ID: {result})")
    else:
        print(f"\n  [失败] {result}")

    input("\n  按回车返回...")


def _modify_group():
    """修改组"""
    clear_screen()
    print_header("修改组")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组\n")
        input("  按回车返回...")
        return

    for g in groups:
        print(f"  [{g['id']}] {g['name']} (限额: {format_gb(g['traffic_limit_mb'])})")

    gid = input_integer("\n  请输入要修改的组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    print(f"\n  当前组名: {group['name']}")
    new_name = input("  新组名 (直接回车保持不变): ").strip()
    if not new_name:
        new_name = None

    print(f"  当前限额: {format_gb(group['traffic_limit_mb'])}")
    new_limit = input_number("  新限额 GB (直接回车保持不变): ", allow_empty=True)
    if new_limit is not None:
        if new_limit <= 0:
            print("  限额必须为正数")
            input("  按回车返回...")
            return
        new_limit = new_limit * 1024  # 转换为 MB 存储

    print(f"  当前通知命令: {group.get('notify_cmd', '-')}")
    new_notify = input("  新通知命令 (回车保持不变，输入 - 清空): ").strip()
    if new_notify == '-':
        new_notify = ''
    elif not new_notify:
        new_notify = None

    success, msg = db.update_group(gid, new_name, new_limit, new_notify)
    print(f"\n  [{('成功' if success else '失败')}] {msg}")
    input("\n  按回车返回...")


def _delete_group():
    """删除组"""
    clear_screen()
    print_header("删除组")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组\n")
        input("  按回车返回...")
        return

    for g in groups:
        vms = db.get_group_vms(g['id'])
        print(f"  [{g['id']}] {g['name']} (限额: {format_gb(g['traffic_limit_mb'])}, VM数: {len(vms)})")

    gid = input_integer("\n  请输入要删除的组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    if confirm_action(f"  确认删除组 '{group['name']}' 及其所有关联数据?"):
        db.delete_group(gid)
        db.insert_action_log('config_change', 'group', gid, f"删除组 '{group['name']}'")
        print("  [成功] 组已删除")

    input("\n  按回车返回...")


def _view_group_vms():
    """查看组内虚拟机"""
    clear_screen()
    print_header("查看组内虚拟机")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组\n")
        input("  按回车返回...")
        return

    for g in groups:
        print(f"  [{g['id']}] {g['name']}")

    gid = input_integer("\n  请选择组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    vms = db.get_group_vms(gid)
    print(f"\n  组 '{group['name']}' 的虚拟机 ({len(vms)} 台):")

    if vms:
        headers = ['VMID', '类型', '名称', '加入时间']
        rows = []
        for v in vms:
            type_label = 'KVM' if v['vm_type'] == 'qemu' else 'LXC'
            rows.append([
                str(v['vm_id']),
                type_label,
                v['vm_name'],
                v['added_at']
            ])
        print_table(headers, rows)

        # 显示流量信息
        print("\n  流量详情:")
        traffic_headers = ['VMID', '类型', '入站', '出站', '合计', '限额', '使用率']
        traffic_rows = []
        for v in vms:
            summary = db.get_vm_traffic_summary(v['vm_id'], v['vm_type'], gid)
            if summary:
                total = summary['total_in_mb'] + summary['total_out_mb']
                pct = (total / group['traffic_limit_mb'] * 100) if group['traffic_limit_mb'] > 0 else 0
                type_label = 'KVM' if v['vm_type'] == 'qemu' else 'LXC'
                traffic_rows.append([
                    str(v['vm_id']),
                    type_label,
                    format_gb(summary['total_in_mb']),
                    format_gb(summary['total_out_mb']),
                    format_gb(total),
                    format_gb(group['traffic_limit_mb']),
                    f"{pct:.1f}%"
                ])
        if traffic_rows:
            print_table(traffic_headers, traffic_rows)

    input("\n  按回车返回...")


# ============================================================
#  虚拟机管理菜单
# ============================================================

def menu_vm_management():
    """虚拟机管理子菜单"""
    while True:
        clear_screen()
        print_header("虚拟机管理")

        managed = db.get_all_managed_vms()
        print(f"  当前管理 {len(managed)} 台虚拟机\n")

        print("  1. 扫描并列出所有虚拟机")
        print("  2. 将虚拟机加入组 (支持 100-110 范围)")
        print("  3. 从组中移除虚拟机")
        print("  4. 查看所有已管理虚拟机")
        print("  0. 返回主菜单")

        choice = input_choice("  请选择 [0-4]: ", ['0', '1', '2', '3', '4'])

        if choice == '0':
            break
        elif choice == '1':
            _scan_vms()
        elif choice == '2':
            _add_vms_to_group()
        elif choice == '3':
            _remove_vm_from_group()
        elif choice == '4':
            _view_managed_vms()


def _scan_vms():
    """扫描所有虚拟机"""
    clear_screen()
    print_header("扫描虚拟机")

    print("  正在扫描 PVE 虚拟机...")
    all_vms = pve.get_all_vms()

    if not all_vms:
        print("\n  未发现虚拟机，请确认:")
        print("  1. 当前在 PVE 节点上运行")
        print("  2. pvesh 命令可用")
        input("\n  按回车返回...")
        return

    # 顺便修复旧版本未保存名称的问题，并同步 PVE 中的名称变更。
    for vm in all_vms:
        db.update_vm_name(vm['vmid'], vm['type'], vm.get('name', ''))

    qemu_vms = [v for v in all_vms if v['type'] == 'qemu']
    lxc_vms = [v for v in all_vms if v['type'] == 'lxc']

    managed_vms = db.get_all_managed_vms()
    managed_ids = {(v['vm_id'], v['vm_type']) for v in managed_vms}

    print(f"\n  KVM 虚拟机 ({len(qemu_vms)} 台):")
    if qemu_vms:
        headers = ['VMID', '名称', '状态', '已管理']
        rows = []
        for v in qemu_vms:
            managed = '是' if (v['vmid'], v['type']) in managed_ids else '否'
            rows.append([str(v['vmid']), v['name'], v['status'], managed])
        print_table(headers, rows)

    print(f"\n  LXC 容器 ({len(lxc_vms)} 台):")
    if lxc_vms:
        headers = ['VMID', '名称', '状态', '已管理']
        rows = []
        for v in lxc_vms:
            managed = '是' if (v['vmid'], v['type']) in managed_ids else '否'
            rows.append([str(v['vmid']), v['name'], v['status'], managed])
        print_table(headers, rows)

    print(f"\n  总计: {len(all_vms)} 台 (KVM: {len(qemu_vms)}, LXC: {len(lxc_vms)})")
    input("\n  按回车返回...")


def _add_vms_to_group():
    """将虚拟机加入组"""
    clear_screen()
    print_header("虚拟机加入组")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组，请先在「组管理」中创建\n")
        input("  按回车返回...")
        return

    print("  可用组:")
    for g in groups:
        vms = db.get_group_vms(g['id'])
        print(f"  [{g['id']}] {g['name']} (限额: {format_gb(g['traffic_limit_mb'])}, 已有VM: {len(vms)})")

    gid = input_integer("\n  请选择目标组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    print(f"\n  目标组: {group['name']}")
    print("  输入格式示例: 100 或 100-110 或 100,102,105-110")
    all_vms = pve.get_all_vms()
    vms = input_vm_range("  请输入要加入的虚拟机: ", all_vms)

    if not vms:
        print("  未选择任何虚拟机")
        input("  按回车返回...")
        return

    print(f"\n  将加入 {len(vms)} 台虚拟机:")
    for vmid, vm_type in vms:
        type_label = 'KVM' if vm_type == 'qemu' else 'LXC'
        print(f"    {type_label} VMID {vmid}")

    if not confirm_action("  确认加入?"):
        print("  已取消")
        input("  按回车返回...")
        return

    monitor_lock = db.acquire_monitor_lock()
    if monitor_lock is None:
        print("  [忙碌] 后台监控正在采样，请稍后重试")
        input("  按回车返回...")
        return

    success_count = 0
    vm_info = {(vm['vmid'], vm['type']): vm for vm in all_vms}
    try:
        for vmid, vm_type in vms:
            # 获取该VM当前PVE流量计数器，作为初始流量
            initial_in_mb = 0
            initial_out_mb = 0
            netin, netout, vm_status, boot_time = pve.get_vm_network_snapshot(vmid, vm_type)
            baseline_in = None
            baseline_out = None
            if vm_status == 'running' and netin is not None and netout is not None:
                initial_in_mb = netin / (1024 * 1024)
                initial_out_mb = netout / (1024 * 1024)
                baseline_in = netin
                baseline_out = netout

            success, msg = db.add_vm_to_group(
                gid, vmid, vm_type,
                vm_name=vm_info.get((vmid, vm_type), {}).get('name', ''),
                initial_in_mb=initial_in_mb,
                initial_out_mb=initial_out_mb,
                baseline_in_bytes=baseline_in,
                baseline_out_bytes=baseline_out,
                boot_time=boot_time
            )
            if success:
                success_count += 1
                print(f"  [成功] {vm_type.upper()} {vmid} 已加入组 '{group['name']}' (初始: {(initial_in_mb + initial_out_mb) / 1024:.1f} GB)")
            else:
                print(f"  [跳过] {vm_type.upper()} {vmid}: {msg}")
    finally:
        db.release_monitor_lock(monitor_lock)

    db.insert_action_log('config_change', 'group', gid,
                         f"添加 {success_count} 台虚拟机到组 '{group['name']}'")
    print(f"\n  成功加入 {success_count}/{len(vms)} 台")
    input("  按回车返回...")


def _remove_vm_from_group():
    """从组中移除虚拟机"""
    clear_screen()
    print_header("从组中移除虚拟机")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组\n")
        input("  按回车返回...")
        return

    for g in groups:
        vms = db.get_group_vms(g['id'])
        print(f"  [{g['id']}] {g['name']} ({len(vms)} 台虚拟机)")

    gid = input_integer("\n  请选择组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    vms = db.get_group_vms(gid)
    if not vms:
        print(f"\n  组 '{group['name']}' 内没有虚拟机")
        input("  按回车返回...")
        return

    print(f"\n  组 '{group['name']}' 的虚拟机:")
    headers = ['序号', 'VMID', '类型', '名称']
    rows = []
    for i, v in enumerate(vms):
        type_label = 'KVM' if v['vm_type'] == 'qemu' else 'LXC'
        rows.append([str(i + 1), str(v['vm_id']), type_label, v['vm_name']])
    print_table(headers, rows)

    # 使用组内记录解析，确保已从 PVE 删除的 VM 仍可解除管理。
    vms_to_remove = input_vm_range(
        "  请输入要移除的虚拟机 (支持范围): ", vms
    )

    if not vms_to_remove:
        print("  未选择任何虚拟机")
        input("  按回车返回...")
        return

    for vmid, vm_type in vms_to_remove:
        db.remove_vm_from_group(gid, vmid, vm_type)
        print(f"  [成功] {vm_type.upper()} {vmid} 已从组 '{group['name']}' 移除")

    input("\n  按回车返回...")


def _view_managed_vms():
    """查看所有已管理虚拟机"""
    clear_screen()
    print_header("已管理虚拟机")

    managed = db.get_all_managed_vms()
    if not managed:
        print("\n  暂无已管理的虚拟机\n")
        input("  按回车返回...")
        return

    headers = ['VMID', '类型', '名称', '所属组']
    rows = []
    for v in managed:
        type_label = 'KVM' if v['vm_type'] == 'qemu' else 'LXC'
        rows.append([
            str(v['vm_id']),
            type_label,
            v['vm_name'],
            v['group_names']
        ])
    print_table(headers, rows)

    print(f"\n  共 {len(managed)} 台虚拟机")
    input("\n  按回车返回...")


# ============================================================
#  流量监控菜单
# ============================================================

def menu_traffic_monitor():
    """流量监控子菜单"""
    while True:
        clear_screen()
        print_header("流量监控")

        print("  1. 查看所有组流量概览")
        print("  2. 查看指定组详细流量")
        print("  3. 查看单台虚拟机流量")
        print("  4. 手动重置组流量")
        print("  5. 查看历史流量记录")
        print("  0. 返回主菜单")

        choice = input_choice("  请选择 [0-5]: ", ['0', '1', '2', '3', '4', '5'])

        if choice == '0':
            break
        elif choice == '1':
            _traffic_overview()
        elif choice == '2':
            _traffic_group_detail()
        elif choice == '3':
            _traffic_vm_detail()
        elif choice == '4':
            _reset_traffic()
        elif choice == '5':
            _traffic_history()


def _traffic_overview():
    """查看流量概览"""
    clear_screen()
    print_header("流量概览")

    overview = db.get_all_traffic_overview()
    if not overview:
        print("\n  暂无数据\n")
        input("  按回车返回...")
        return

    headers = ['组ID', '组名', 'VM数', '组总流量', '单VM限额', '超限VM']
    rows = []
    for g in overview:
        group = db.get_group_by_id(g['group_id'])
        limit = group['traffic_limit_mb'] if group else 0

        # 检查超限 VM 数量
        vms = db.get_group_traffic_overview(g['group_id'])
        over_count = sum(1 for v in vms if (v['total_in_mb'] + v['total_out_mb']) >= limit)

        rows.append([
            str(g['group_id']),
            g['group_name'],
            str(g['vm_count']),
            format_gb(g['total_traffic']),
            format_gb(limit),
            f"{over_count}/{g['vm_count']}" if g['vm_count'] > 0 else '-'
        ])
    print_table(headers, rows)

    input("\n  按回车返回...")


def _traffic_group_detail():
    """查看指定组详细流量"""
    clear_screen()
    print_header("组流量详情")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组\n")
        input("  按回车返回...")
        return

    for g in groups:
        print(f"  [{g['id']}] {g['name']}")

    gid = input_integer("\n  请选择组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    vms = db.get_group_traffic_overview(gid)
    print(f"\n  组: {group['name']} | 单VM限额: {format_gb(group['traffic_limit_mb'])}")
    print(f"  通知命令: {group.get('notify_cmd', '(未设置)')}")

    if not vms:
        print("\n  组内没有虚拟机\n")
        input("  按回车返回...")
        return

    headers = ['VMID', '名称', '入站', '出站', '合计', '使用率', '状态']
    rows = []
    for v in vms:
        total = v['total_in_mb'] + v['total_out_mb']
        pct = (total / group['traffic_limit_mb'] * 100) if group['traffic_limit_mb'] > 0 else 0
        limit = group['traffic_limit_mb']
        status_icon = '[超限]' if total >= limit else '[正常]'
        rows.append([
            str(v['vm_id']),
            v.get('vm_name', ''),
            format_gb(v['total_in_mb']),
            format_gb(v['total_out_mb']),
            format_gb(total),
            f"{pct:.1f}%",
            status_icon
        ])
    print_table(headers, rows)

    # 汇总
    total_all = sum(v['total_in_mb'] + v['total_out_mb'] for v in vms)
    over_count = sum(1 for v in vms if (v['total_in_mb'] + v['total_out_mb']) >= group['traffic_limit_mb'])
    print(f"\n  组总流量: {format_gb(total_all)} | 超限VM: {over_count}/{len(vms)}")

    input("\n  按回车返回...")


def _traffic_vm_detail():
    """查看单台虚拟机流量"""
    clear_screen()
    print_header("虚拟机流量详情")

    managed = db.get_all_managed_vms()
    if not managed:
        print("\n  暂无已管理虚拟机\n")
        input("  按回车返回...")
        return

    print("  已管理虚拟机:")
    for i, v in enumerate(managed):
        print(f"  [{i + 1}] {v['vm_type'].upper()} {v['vm_id']} - {v['vm_name']} (组: {v['group_names']})")

    idx = input_integer("\n  请选择虚拟机 (输入VMID): ")
    if idx is None:
        return

    vm_info = None
    vm_groups = []

    for v in managed:
        if v['vm_id'] == idx:
            vm_info = v
            vtype = v['vm_type']
            vm_groups = [dict(r) for r in db.get_vm_groups(v['vm_id'], vtype)]
            break

    if not vm_info:
        print(f"  未找到 VMID {idx}")
        input("  按回车返回...")
        return

    print(f"\n  {vm_info['vm_type'].upper()} {vm_info['vm_id']} - {vm_info['vm_name']}")
    print(f"  所属组: {vm_info['group_names']}")
    print()

    # 在各个组中的流量
    for vg in vm_groups:
        summary = db.get_vm_traffic_summary(vm_info['vm_id'], vm_info['vm_type'], vg['group_id'])
        if summary:
            total = summary['total_in_mb'] + summary['total_out_mb']
            pct = (total / vg['traffic_limit_mb'] * 100) if vg['traffic_limit_mb'] > 0 else 0
            print(f"  组 '{vg['group_name']}':")
            print(f"    入站: {format_gb(summary['total_in_mb'])} | 出站: {format_gb(summary['total_out_mb'])}")
            print(f"    合计: {format_gb(total)} | 限额: {format_gb(vg['traffic_limit_mb'])} | 使用率: {pct:.1f}%")
            print(f"    上次重置: {summary['last_reset']}")

    # 最近日志
    logs = db.get_traffic_logs(vm_id=vm_info['vm_id'], vm_type=vm_info['vm_type'], limit=5)
    if logs:
        print(f"\n  最近 5 条流量记录:")
        headers = ['时间', 'netin', 'netout', 'Δ入站', 'Δ出站']
        rows = []
        for log in logs:
            rows.append([
                log['timestamp'],
                format_bytes(log['netin_bytes']),
                format_bytes(log['netout_bytes']),
                format_bytes(log['delta_in_bytes']),
                format_bytes(log['delta_out_bytes'])
            ])
        print_table(headers, rows)

    input("\n  按回车返回...")


def _reset_traffic():
    """手动重置组流量"""
    clear_screen()
    print_header("重置组流量")

    groups = db.get_all_groups()
    if not groups:
        print("\n  暂无管理组\n")
        input("  按回车返回...")
        return

    for g in groups:
        vms = db.get_group_traffic_overview(g['id'])
        total = sum(v['total_in_mb'] + v['total_out_mb'] for v in vms)
        print(f"  [{g['id']}] {g['name']} (当前总流量: {format_gb(total)})")

    gid = input_integer("\n  请选择要重置的组ID: ")
    if gid is None:
        return

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    if confirm_action(f"  确认重置组 '{group['name']}' 的所有流量数据?"):
        db.reset_group_traffic(gid)
        db.insert_action_log('reset', 'group', gid, f"手动重置组 '{group['name']}' 流量")
        print(f"  [成功] 组 '{group['name']}' 流量已重置")
    else:
        print("  已取消")

    input("\n  按回车返回...")


def _traffic_history():
    """查看历史流量记录"""
    clear_screen()
    print_header("历史流量记录")

    managed = db.get_all_managed_vms()
    if managed:
        print("  可按 VMID 筛选，或直接回车查看所有记录")
        vmid = input_integer("  VMID (可选): ", allow_empty=True)
    else:
        vmid = None

    logs = db.get_traffic_logs(vm_id=vmid, limit=50)

    if not logs:
        print("\n  暂无流量记录\n")
        input("  按回车返回...")
        return

    print(f"\n  最近 {len(logs)} 条记录:\n")
    headers = ['时间', 'VMID', '类型', 'netin', 'netout', 'Δ入站', 'Δ出站']
    rows = []
    for log in logs:
        type_label = 'KVM' if log['vm_type'] == 'qemu' else 'LXC'
        rows.append([
            log['timestamp'],
            str(log['vm_id']),
            type_label,
            format_bytes(log['netin_bytes']),
            format_bytes(log['netout_bytes']),
            format_bytes(log['delta_in_bytes']),
            format_bytes(log['delta_out_bytes'])
        ])
    print_table(headers, rows)

    input("\n  按回车返回...")


# ============================================================
#  系统设置菜单
# ============================================================

def _upgrade_call():
    """调用 upgrade.py 进行升级"""
    clear_screen()
    print_header("检查并升级程序")

    upgrade_script = os.path.join(BASE_DIR, "upgrade.py")
    if not os.path.exists(upgrade_script):
        print("\n  未找到 upgrade.py，请确保文件完整\n")
        input("  按回车返回...")
        return

    print(f"  正在启动升级程序...")
    print(f"  命令: {PYTHON_PATH} {upgrade_script}")
    print()

    try:
        proc = subprocess.run(
            [PYTHON_PATH, upgrade_script],
            cwd=BASE_DIR
        )
        if proc.returncode == 0:
            print(f"\n  [完成] 升级程序执行完毕")
        else:
            print(f"\n  [退出] 升级程序返回码: {proc.returncode}")
    except Exception as e:
        print(f"\n  [错误] {e}")

    input("\n  按回车返回...")


def menu_settings():
    """系统设置子菜单"""
    while True:
        clear_screen()
        print_header("系统设置")

        # 显示 crontab 状态和快捷指令状态
        installed, current = _crontab_status()
        crontab_status = "[已安装]" if installed else "[未安装]"
        shortcut_status = "[已安装]" if _shortcut_status() else "[未安装]"
        tg_settings = db.get_telegram_settings()
        telegram_status = (
            "[已启用]" if tg_settings.get('enabled') else
            "[已配置]" if tg_settings.get('bot_token') and tg_settings.get('chat_id') else
            "[未配置]"
        )

        print(f"  1. 查看当前配置")
        print(f"  2. 后台监控管理 {crontab_status}")
        print(f"  3. 查看操作日志")
        print(f"  4. 检查并升级程序")
        print(f"  5. 快捷指令管理 {shortcut_status}")
        print(f"  6. Telegram 接入 {telegram_status}")
        print(f"  0. 返回主菜单")

        choice = input_choice("  请选择 [0-6]: ", ['0', '1', '2', '3', '4', '5', '6'])

        if choice == '0':
            break
        elif choice == '1':
            _view_config()
        elif choice == '2':
            _crontab_manage()
        elif choice == '3':
            _view_action_logs()
        elif choice == '4':
            _upgrade_call()
        elif choice == '5':
            _shortcut_manage()
        elif choice == '6':
            _telegram_manage()


def _telegram_service_label():
    if telegram_service is None:
        return '[组件缺失]'
    status = telegram_service.get_bot_service_status()
    if not status['supported']:
        return '[不支持]'
    if status['active']:
        return '[运行中]'
    if status['enabled']:
        return '[已启用/未运行]'
    return '[未安装]'


def _print_telegram_diagnostics(result):
    if telegram_service is None:
        print('  Telegram 组件缺失；请运行 python3 upgrade.py --force 补齐程序文件。')
        return
    print()
    for line in telegram_service.format_startup_status(result):
        print(f'  {line}')
    print(
        f"  python-telegram-bot: "
        f"{'[已安装] v' + result['dependency_detail'] if result['dependency_ok'] else '[未安装]'}"
    )


def _telegram_run_diagnostics(wait=True):
    clear_screen()
    print_header('Telegram 连接诊断')
    if telegram_service is None:
        _print_telegram_diagnostics({})
        if wait:
            input('\n  按回车返回...')
        return None
    print('  正在测试 Telegram HTTPS 网络、Bot Token 和授权会话…')
    try:
        result = telegram_service.diagnose_startup()
        _print_telegram_diagnostics(result)
    except Exception as exc:
        result = None
        token = db.get_telegram_settings().get('bot_token', '')
        print(f'\n  [失败] 诊断异常: {telegram_service._safe_error(exc, token)}')
    if wait:
        input('\n  按回车返回...')
    return result


def _telegram_manage():
    """Telegram 配置、诊断、依赖和长轮询服务管理。"""
    while True:
        clear_screen()
        print_header('Telegram 接入管理')
        settings = db.get_telegram_settings()
        if telegram_service is None:
            masked = '[组件缺失]'
            dependency = '[未知]'
        else:
            masked = telegram_service.mask_token(settings.get('bot_token'))
            dep_ok, dep_version = telegram_service.dependency_status()
            dependency = f'[已安装] v{dep_version}' if dep_ok else '[未安装]'

        print(f"  Bot Token: {masked}")
        print(f"  推送会话 ID: {settings.get('chat_id') or '[未设置]'}")
        print(f"  消息推送: {'[已启用]' if settings.get('enabled') else '[未启用]'}")
        print(f"  预警阈值: {settings.get('warning_percent', 80):g}%")
        print(f"  Python 依赖: {dependency}")
        print(f"  Bot 后台服务: {_telegram_service_label()}")
        print()
        print('  1. 设置/更换 Bot Token')
        print('  2. 设置推送与授权会话 ID')
        print('  3. 启用/停用 Telegram 推送')
        print('  4. 修改流量预警比例')
        print('  5. 运行网络、Token 与会话诊断')
        print('  6. 发送测试消息')
        print('  7. 安装/更新 Telegram Python 依赖')
        print('  8. 安装/重启 Bot 后台服务')
        print('  9. 卸载 Bot 后台服务')
        print('  0. 返回')

        choice = input_choice('  请选择 [0-9]: ', [str(i) for i in range(10)])
        if choice == '0':
            break
        if choice == '1':
            try:
                token = getpass.getpass('  输入 Bot Token（输入 clear 清除，留空取消）: ').strip()
            except (EOFError, KeyboardInterrupt):
                token = ''
            if token:
                value = '' if token.lower() == 'clear' else token
                ok, message = db.update_telegram_settings(bot_token=value)
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
                if ok:
                    db.insert_action_log(
                        'config_change', target_type='system',
                        detail='更新 Telegram Bot Token（内容已隐藏）',
                    )
            else:
                print('\n  已取消')
            input('\n  按回车继续...')
        elif choice == '2':
            chat_id = input('  输入会话 ID（群组通常为负数，输入 clear 清除）: ').strip()
            value = '' if chat_id.lower() == 'clear' else chat_id
            ok, message = db.update_telegram_settings(chat_id=value)
            print(f"\n  [{'成功' if ok else '失败'}] {message}")
            if ok:
                db.insert_action_log(
                    'config_change', target_type='system',
                    detail=f"更新 Telegram 会话 ID: {value or '[已清除]'}",
                )
            input('\n  按回车继续...')
        elif choice == '3':
            target = not bool(settings.get('enabled'))
            if target and (not settings.get('bot_token') or not settings.get('chat_id')):
                print('\n  [失败] 请先配置 Bot Token 和会话 ID')
            else:
                ok, message = db.update_telegram_settings(enabled=target)
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
                if ok:
                    db.insert_action_log(
                        'config_change', target_type='system',
                        detail=f"{'启用' if target else '停用'} Telegram 推送",
                    )
            input('\n  按回车继续...')
        elif choice == '4':
            percent = input_number('  输入预警比例 (1-99，默认 80): ')
            if percent is not None:
                ok, message = db.update_telegram_settings(warning_percent=percent)
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
                if ok:
                    db.insert_action_log(
                        'config_change', target_type='system',
                        detail=f'设置 Telegram 流量预警比例为 {percent:g}%',
                    )
            input('\n  按回车继续...')
        elif choice == '5':
            _telegram_run_diagnostics()
        elif choice == '6':
            if telegram_service is None:
                print('\n  [失败] Telegram 组件缺失')
            else:
                ok, message = telegram_service.send_message(
                    '✅ PVE Traffic Manager Telegram 测试成功。', force=True
                )
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
            input('\n  按回车继续...')
        elif choice == '7':
            if telegram_service is None:
                print('\n  [失败] Telegram 组件缺失，请先补齐升级文件')
            elif confirm_action('  将通过 pip 安装 requirements.txt，继续?'):
                print('  正在安装依赖，请稍候…')
                ok, message = telegram_service.install_dependency()
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
            input('\n  按回车继续...')
        elif choice == '8':
            if telegram_service is None:
                print('\n  [失败] Telegram 组件缺失')
            else:
                ok, message = telegram_service.install_bot_service()
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
                if ok:
                    db.insert_action_log(
                        'config_change', target_type='system',
                        detail='安装/重启 Telegram Bot systemd 服务',
                    )
            input('\n  按回车继续...')
        elif choice == '9':
            if telegram_service is None:
                print('\n  [失败] Telegram 组件缺失')
            elif confirm_action('  确认停止并卸载 Telegram Bot 后台服务?'):
                ok, message = telegram_service.uninstall_bot_service()
                print(f"\n  [{'成功' if ok else '失败'}] {message}")
                if ok:
                    db.insert_action_log(
                        'config_change', target_type='system',
                        detail='卸载 Telegram Bot systemd 服务',
                    )
            input('\n  按回车继续...')


def _read_crontab():
    """安全读取 crontab；区分“尚无任务”和真实读取错误。"""
    try:
        result = subprocess.run(
            ['crontab', '-l'], capture_output=True, text=True, timeout=10
        )
    except Exception as exc:
        return False, "", str(exc)
    if result.returncode == 0:
        return True, result.stdout.strip(), ""
    error = result.stderr.strip()
    if result.returncode == 1 and 'no crontab for' in error.lower():
        return True, "", ""
    return False, "", error or f"crontab -l 返回码 {result.returncode}"


def _crontab_status():
    """检查 crontab 是否已安装"""
    ok, stdout, _ = _read_crontab()
    if not ok:
        return False, ""
    matching = [
        line.strip() for line in stdout.splitlines()
        if CRONTAB_MARKER in line
    ]
    for line in matching:
        if line and not line.startswith('#'):
            return True, line
    return False, ""


def _crontab_manage():
    """后台监控管理子菜单"""
    while True:
        clear_screen()
        print_header("后台监控管理")

        installed, entry = _crontab_status()

        if installed:
            print(f"  状态: [已安装]")
            print(f"  当前任务: {entry}")
        else:
            print(f"  状态: [未安装]")
            print(f"  建议任务: */{DEFAULT_MONITOR_INTERVAL} * * * * {PYTHON_PATH} {MONITOR_SCRIPT}")

        print()
        if installed:
            print("  1. 重新安装 (更新间隔)")
            print("  2. 卸载后台监控")
            print("  3. 手动执行一次监控 (前台)")
        else:
            print("  1. 安装后台监控")
            print("  2. 手动执行一次监控 (前台)")

        print("  0. 返回")

        choices = ['0', '1', '2', '3'] if installed else ['0', '1', '2']
        choice = input_choice("  请选择: ", choices)

        if choice == '0':
            break
        elif choice == '1':
            if installed:
                _crontab_install()
            else:
                _crontab_install()
        elif choice == '2' and installed:
            _crontab_uninstall()
        elif choice == '2' and not installed or choice == '3' and installed:
            _crontab_run_once()


def _crontab_install():
    """安装/更新 crontab 任务"""
    clear_screen()
    print_header("安装后台监控")

    # 选择间隔
    interval = input_integer(
        f"  监控间隔(分钟，1-59) [默认{DEFAULT_MONITOR_INTERVAL}]: ",
        allow_empty=True
    )
    if interval is None:
        interval = DEFAULT_MONITOR_INTERVAL
    if not 1 <= interval <= 59:
        print("\n  [失败] crontab 的分钟间隔必须在 1-59 之间")
        input("\n  按回车返回...")
        return

    cron_line = (
        f"*/{interval} * * * * {shlex.quote(PYTHON_PATH)} "
        f"{shlex.quote(MONITOR_SCRIPT)} {CRONTAB_MARKER}"
    )

    # 读取现有 crontab
    ok, stdout, error = _read_crontab()
    if not ok:
        print(f"\n  [失败] 无法读取现有 crontab，未做任何修改: {error}")
        input("\n  按回车返回...")
        return
    existing_lines = stdout.splitlines() if stdout else []

    # 移除旧条目
    new_lines = [l for l in existing_lines if CRONTAB_MARKER not in l]

    # 添加新条目
    new_lines.append("")
    new_lines.append(cron_line)
    new_lines.append("")

    # 写回 crontab
    crontab_content = "\n".join(new_lines) + "\n"
    try:
        proc = subprocess.run(['crontab', '-'], input=crontab_content,
                            capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            print(f"\n  [成功] 后台监控已安装 (每 {interval} 分钟)")
            print(f"  任务: {cron_line}")
            db.insert_action_log('config_change', target_type='system',
                               detail=f"安装 crontab 监控 (间隔 {interval} 分钟)")
        else:
            print(f"\n  [失败] {proc.stderr.strip()}")
    except Exception as e:
        print(f"\n  [失败] {e}")

    input("\n  按回车返回...")


def _crontab_uninstall():
    """卸载 crontab 任务"""
    clear_screen()
    print_header("卸载后台监控")

    if not confirm_action("  确认卸载后台监控任务?"):
        print("  已取消")
        input("  按回车返回...")
        return

    ok, stdout, error = _read_crontab()
    if not ok:
        print(f"\n  [失败] 无法读取现有 crontab，未做任何修改: {error}")
        input("\n  按回车返回...")
        return
    existing_lines = stdout.splitlines() if stdout else []

    new_lines = [l for l in existing_lines if CRONTAB_MARKER not in l]

    # 清理多余空行
    while new_lines and new_lines[0] == "":
        new_lines.pop(0)
    while new_lines and new_lines[-1] == "":
        new_lines.pop()

    crontab_content = "\n".join(new_lines) + "\n" if new_lines else ""

    try:
        if crontab_content.strip():
            proc = subprocess.run(['crontab', '-'], input=crontab_content,
                                capture_output=True, text=True, timeout=10)
        else:
            # 空 crontab: 移除所有内容
            proc = subprocess.run(['crontab', '-r'],
                                capture_output=True, text=True, timeout=10)

        if proc.returncode == 0:
            print("\n  [成功] 后台监控已卸载")
            db.insert_action_log('config_change', target_type='system',
                               detail="卸载 crontab 监控")
        else:
            print(f"\n  [失败] {proc.stderr.strip()}")
    except Exception as e:
        print(f"\n  [失败] {e}")

    input("\n  按回车返回...")


def _crontab_run_once():
    """手动执行一次监控"""
    clear_screen()
    print_header("手动执行监控")

    print("  正在执行一次流量监控...")
    print(f"  命令: {PYTHON_PATH} {MONITOR_SCRIPT}")
    print()

    try:
        proc = subprocess.run(
            [PYTHON_PATH, MONITOR_SCRIPT],
            capture_output=True, text=True
        )
        output = proc.stdout.strip()
        errors = proc.stderr.strip()

        if output:
            print(output)
        if errors:
            print(f"  [stderr]\n{errors}")

        if proc.returncode == 0:
            print(f"\n  [完成] 监控执行完毕")
        else:
            print(f"\n  [警告] 执行返回码: {proc.returncode}")
    except Exception as e:
        print(f"  [错误] {e}")

    input("\n  按回车返回...")


SHORTCUT_CMD = "ptm"
SHORTCUT_PATH = f"/usr/local/bin/{SHORTCUT_CMD}"


def _shortcut_manage():
    """快捷指令管理子菜单"""
    while True:
        clear_screen()
        print_header("快捷指令管理")

        if _shortcut_status():
            print(f"  状态: [已安装] 在任意目录输入 ptm 即可启动")
            print()
            print("  1. 重新安装快捷指令")
            print("  2. 卸载快捷指令")
            print("  0. 返回")
            choice = input_choice("  请选择: ", ['0', '1', '2'])
        else:
            print(f"  状态: [未安装]")
            print()
            print("  1. 安装快捷指令 (ptm)")
            print("  0. 返回")
            choice = input_choice("  请选择: ", ['0', '1'])

        if choice == '0':
            break
        elif choice == '1':
            _shortcut_install()
        elif choice == '2':
            _shortcut_uninstall()


def _shortcut_status():
    """检查快捷指令是否已安装"""
    return os.path.exists(SHORTCUT_PATH)


def _shortcut_install():
    """安装快捷指令 ptm"""
    clear_screen()
    print_header("安装快捷指令")

    if _shortcut_status():
        print(f"  快捷指令 '{SHORTCUT_CMD}' 已安装")
        if confirm_action("  确认覆盖重新安装?"):
            pass
        else:
            print("  已取消")
            input("  按回车返回...")
            return

    script_content = f"""#!/bin/bash
# pve-traffic-manager shortcut
cd {shlex.quote(BASE_DIR)} && exec {shlex.quote(PYTHON_PATH)} {shlex.quote(os.path.join(BASE_DIR, 'manager.py'))} "$@"
"""

    try:
        with open(SHORTCUT_PATH, "w") as f:
            f.write(script_content)
        os.chmod(SHORTCUT_PATH, 0o755)

        print(f"\n  [成功] 快捷指令 '{SHORTCUT_CMD}' 已安装")
        print(f"  现在可以在任意目录直接输入 ptm 启动程序")
        db.insert_action_log('config_change', target_type='system',
                           detail=f"安装快捷指令 {SHORTCUT_CMD}")
    except PermissionError:
        print(f"\n  [失败] 权限不足，请使用 root 运行")
    except Exception as e:
        print(f"\n  [失败] {e}")

    input("\n  按回车返回...")


def _shortcut_uninstall():
    """卸载快捷指令 ptm"""
    clear_screen()
    print_header("卸载快捷指令")

    if not _shortcut_status():
        print(f"  快捷指令 '{SHORTCUT_CMD}' 未安装")
        input("  按回车返回...")
        return

    if not confirm_action(f"  确认卸载快捷指令 '{SHORTCUT_CMD}'?"):
        print("  已取消")
        input("  按回车返回...")
        return

    try:
        os.remove(SHORTCUT_PATH)
        print(f"\n  [成功] 快捷指令 '{SHORTCUT_CMD}' 已卸载")
        db.insert_action_log('config_change', target_type='system',
                           detail=f"卸载快捷指令 {SHORTCUT_CMD}")
    except Exception as e:
        print(f"\n  [失败] {e}")

    input("\n  按回车返回...")


def _view_config():
    """查看当前配置"""
    clear_screen()
    print_header("当前配置")

    groups = db.get_all_groups()
    managed = db.get_all_managed_vms()
    installed, entry = _crontab_status()

    print(f"  PVE 节点: {__import__('config').PVE_NODE}")
    print(f"  数据库路径: {__import__('config').DB_PATH}")
    print(f"  Python 路径: {PYTHON_PATH}")
    print(f"  监控脚本路径: {MONITOR_SCRIPT}")
    print(f"  后台监控: {'[已安装] ' + entry if installed else '[未安装]'}")
    print(f"  管理组数量: {len(groups)}")
    print(f"  管理虚拟机: {len(managed)}")

    tg = db.get_telegram_settings()
    token_display = (
        telegram_service.mask_token(tg.get('bot_token'))
        if telegram_service is not None else '[组件缺失]'
    )
    print(f"  Telegram Token: {token_display}")
    print(f"  Telegram 会话: {tg.get('chat_id') or '[未设置]'}")
    print(f"  Telegram 推送: {'[已启用]' if tg.get('enabled') else '[未启用]'}")
    print(f"  Telegram 预警: {tg.get('warning_percent', 80):g}%")

    print(f"\n  各组流量限额:")
    for g in groups:
        vms = db.get_group_vms(g['id'])
        print(f"    {g['name']}: {format_gb(g['traffic_limit_mb'])}/台 ({len(vms)} 台)")

    print(f"\n  通知配置:")
    for g in groups:
        cmd = g.get('notify_cmd', '')
        print(f"    {g['name']}: {'[已配置] ' + cmd if cmd else '[未配置]'}")

    input("\n  按回车返回...")


def _view_action_logs():
    """查看操作日志"""
    clear_screen()
    print_header("操作日志")

    logs = db.get_action_logs(limit=50)
    if not logs:
        print("\n  暂无操作日志\n")
        input("  按回车返回...")
        return

    headers = ['时间', '操作', '目标类型', '目标ID', '详情']
    rows = []
    for log in logs:
        detail = log.get('detail', '')
        if len(detail) > 50:
            detail = detail[:47] + '...'
        rows.append([
            log['created_at'],
            log['action'],
            log.get('target_type', '-'),
            str(log.get('target_id', '-')),
            detail
        ])
    print_table(headers, rows)

    input("\n  按回车返回...")


# ============================================================
#  主菜单
# ============================================================

def main_menu(startup_telegram=None):
    """主菜单循环"""
    while True:
        clear_screen()
        print_header("PVE 流量控制管理器")

        groups = db.get_all_groups()
        managed = db.get_all_managed_vms()
        print(f"  管理组: {len(groups)} | 管理虚拟机: {len(managed)}")
        if startup_telegram:
            for line in telegram_service.format_startup_status(startup_telegram):
                print(f'  {line}')
            print(
                f"  Telegram 推送: "
                f"{'[已启用]' if startup_telegram.get('enabled') else '[未启用]'}"
            )
        elif telegram_service is None:
            print('  Telegram: [组件缺失，请运行 upgrade.py --force]')
        print()

        print("  1. 组管理")
        print("  2. 虚拟机管理")
        print("  3. 流量监控")
        print("  4. 系统设置")
        print("  5. 退出")

        choice = input_choice("  请选择 [1-5]: ", ['1', '2', '3', '4', '5'])

        if choice == '1':
            menu_group_management()
        elif choice == '2':
            menu_vm_management()
        elif choice == '3':
            menu_traffic_monitor()
        elif choice == '4':
            menu_settings()
        elif choice == '5':
            print("\n  再见!")
            break


# ============================================================
#  入口
# ============================================================

if __name__ == '__main__':
    # 初始化数据库
    db.init_db()
    startup_telegram = None
    if telegram_service is not None:
        print('正在检查 Telegram 网络、Bot Token 与授权会话…')
        try:
            startup_telegram = telegram_service.diagnose_startup()
        except Exception as exc:
            token = db.get_telegram_settings().get('bot_token', '')
            startup_telegram = {
                'network_ok': False,
                'network_detail': telegram_service._safe_error(exc, token),
                'token_ok': None, 'token_detail': '未测试',
                'chat_ok': None, 'chat_detail': '未测试',
                'enabled': False,
            }
    main_menu(startup_telegram)
