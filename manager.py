# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - CLI 前台交互主程序
用法: python manager.py
"""

import os
import sys
import re
import subprocess
import db
import pve
from config import BASE_DIR, PYTHON_PATH, MONITOR_SCRIPT, DEFAULT_MONITOR_INTERVAL

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


def format_mb(mb):
    """MB 转可读格式"""
    if mb is None:
        return "N/A"
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    else:
        return f"{mb:.2f} MB"


def format_traffic_mb(mb):
    """流量 MB 格式化"""
    return format_mb(mb)


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
            return float(val)
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        except ValueError:
            print("  请输入有效数字")


def input_vm_range(prompt):
    """
    解析虚拟机范围输入
    支持格式: 100, 100-110, 100,102,105-110
    返回: [(vmid, vm_type), ...] 或空列表
    """
    raw = input(prompt).strip()
    if not raw:
        return []

    # 获取所有可用虚拟机用于匹配
    all_vms = pve.get_all_vms()
    vm_map = {}  # key: str(vmid), value: list of (vmid, vm_type)
    for vm in all_vms:
        key = str(vm['vmid'])
        if key not in vm_map:
            vm_map[key] = []
        vm_map[key].append((vm['vmid'], vm['type']))

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
            for vmid in range(start, end + 1):
                key = str(vmid)
                if key in vm_map:
                    for v in vm_map[key]:
                        if v not in result:
                            result.append(v)
                else:
                    print(f"  [警告] VMID {vmid} 不存在，已跳过")
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
                    format_mb(g['traffic_limit_mb']),
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

    limit = input_number("  流量限额 (MB): ")
    if limit is None or limit <= 0:
        print("  限额必须为正数")
        input("  按回车返回...")
        return

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
        print(f"  [{g['id']}] {g['name']} (限额: {format_mb(g['traffic_limit_mb'])})")

    gid = input_number("\n  请输入要修改的组ID: ")
    if gid is None:
        return
    gid = int(gid)

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    print(f"\n  当前组名: {group['name']}")
    new_name = input("  新组名 (直接回车保持不变): ").strip()
    if not new_name:
        new_name = None

    print(f"  当前限额: {format_mb(group['traffic_limit_mb'])}")
    new_limit = input_number("  新限额 MB (直接回车保持不变): ", allow_empty=True)

    print(f"  当前通知命令: {group.get('notify_cmd', '-')}")
    new_notify = input("  新通知命令 (直接回车保持不变): ").strip()
    if not new_notify and group.get('notify_cmd'):
        new_notify = None
    elif not new_notify:
        new_notify = ''

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
        print(f"  [{g['id']}] {g['name']} (限额: {format_mb(g['traffic_limit_mb'])}, VM数: {len(vms)})")

    gid = input_number("\n  请输入要删除的组ID: ")
    if gid is None:
        return
    gid = int(gid)

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

    gid = input_number("\n  请选择组ID: ")
    if gid is None:
        return
    gid = int(gid)

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
                    format_mb(summary['total_in_mb']),
                    format_mb(summary['total_out_mb']),
                    format_mb(total),
                    format_mb(group['traffic_limit_mb']),
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
        print(f"  [{g['id']}] {g['name']} (限额: {format_mb(g['traffic_limit_mb'])}, 已有VM: {len(vms)})")

    gid = input_number("\n  请选择目标组ID: ")
    if gid is None:
        return
    gid = int(gid)

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    print(f"\n  目标组: {group['name']}")
    print("  输入格式示例: 100 或 100-110 或 100,102,105-110")
    vms = input_vm_range("  请输入要加入的虚拟机: ")

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

    success_count = 0
    for vmid, vm_type in vms:
        success, msg = db.add_vm_to_group(gid, vmid, vm_type)
        if success:
            success_count += 1
            print(f"  [成功] {vm_type.upper()} {vmid} 已加入组 '{group['name']}'")
        else:
            print(f"  [跳过] {vm_type.upper()} {vmid}: {msg}")

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

    gid = input_number("\n  请选择组ID: ")
    if gid is None:
        return
    gid = int(gid)

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

    vms_to_remove = input_vm_range("  请输入要移除的虚拟机 (支持范围): ")

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
            format_mb(g['total_traffic']),
            format_mb(limit),
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

    gid = input_number("\n  请选择组ID: ")
    if gid is None:
        return
    gid = int(gid)

    group = db.get_group_by_id(gid)
    if not group:
        print("  组不存在")
        input("  按回车返回...")
        return

    vms = db.get_group_traffic_overview(gid)
    print(f"\n  组: {group['name']} | 单VM限额: {format_mb(group['traffic_limit_mb'])}")
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
            format_mb(v['total_in_mb']),
            format_mb(v['total_out_mb']),
            format_mb(total),
            f"{pct:.1f}%",
            status_icon
        ])
    print_table(headers, rows)

    # 汇总
    total_all = sum(v['total_in_mb'] + v['total_out_mb'] for v in vms)
    over_count = sum(1 for v in vms if (v['total_in_mb'] + v['total_out_mb']) >= group['traffic_limit_mb'])
    print(f"\n  组总流量: {format_mb(total_all)} | 超限VM: {over_count}/{len(vms)}")

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

    idx = input_number("\n  请选择虚拟机 (输入VMID): ")
    if idx is None:
        return
    idx = int(idx)

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
            print(f"    入站: {format_mb(summary['total_in_mb'])} | 出站: {format_mb(summary['total_out_mb'])}")
            print(f"    合计: {format_mb(total)} | 限额: {format_mb(vg['traffic_limit_mb'])} | 使用率: {pct:.1f}%")
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
        print(f"  [{g['id']}] {g['name']} (当前总流量: {format_mb(total)})")

    gid = input_number("\n  请选择要重置的组ID: ")
    if gid is None:
        return
    gid = int(gid)

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
        vmid_input = input("  VMID (可选): ").strip()
        vmid = int(vmid_input) if vmid_input else None
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

def menu_settings():
    """系统设置子菜单"""
    while True:
        clear_screen()
        print_header("系统设置")

        # 显示 crontab 状态
        installed, current = _crontab_status()
        status_text = "[已安装]" if installed else "[未安装]"

        print(f"  1. 查看当前配置")
        print(f"  2. 后台监控管理 {status_text}")
        print(f"  3. 查看操作日志")
        print(f"  0. 返回主菜单")

        choice = input_choice("  请选择 [0-3]: ", ['0', '1', '2', '3'])

        if choice == '0':
            break
        elif choice == '1':
            _view_config()
        elif choice == '2':
            _crontab_manage()
        elif choice == '3':
            _view_action_logs()


def _run(cmd_list, timeout=10):
    """执行命令，返回 (ok, stdout)"""
    try:
        r = subprocess.run(cmd_list, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip()
    except Exception:
        return False, ""


def _crontab_status():
    """检查 crontab 是否已安装"""
    ok, stdout = _run(['crontab', '-l'])
    if not ok:
        return False, ""
    for line in stdout.splitlines():
        if CRONTAB_MARKER in line:
            return True, line.strip()
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
    interval = input_number(f"  监控间隔(分钟) [默认{DEFAULT_MONITOR_INTERVAL}]: ", allow_empty=True)
    if interval is None:
        interval = DEFAULT_MONITOR_INTERVAL
    interval = int(interval)
    if interval < 1:
        interval = 1

    cron_line = f"*/{interval} * * * * {PYTHON_PATH} {MONITOR_SCRIPT} {CRONTAB_MARKER}"

    # 读取现有 crontab
    ok, stdout = _run(['crontab', '-l'])
    existing_lines = stdout.splitlines() if ok and stdout else []

    # 移除旧条目
    new_lines = [l for l in existing_lines if CRONTAB_MARKER not in l]

    # 添加新条目
    new_lines.append("")
    new_lines.append(f"{CRONTAB_MARKER}")
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

    ok, stdout = _run(['crontab', '-l'])
    existing_lines = stdout.splitlines() if ok and stdout else []

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
            capture_output=True, text=True, timeout=120
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
    except subprocess.TimeoutExpired:
        print("  [超时] 监控执行超时")
    except Exception as e:
        print(f"  [错误] {e}")

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

    print(f"\n  各组流量限额:")
    for g in groups:
        vms = db.get_group_vms(g['id'])
        print(f"    {g['name']}: {format_mb(g['traffic_limit_mb'])}/台 ({len(vms)} 台)")

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

def main_menu():
    """主菜单循环"""
    while True:
        clear_screen()
        print_header("PVE 流量控制管理器")

        groups = db.get_all_groups()
        managed = db.get_all_managed_vms()
        print(f"  管理组: {len(groups)} | 管理虚拟机: {len(managed)}")
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
    main_menu()
