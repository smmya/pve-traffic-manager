# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - PVE API 交互封装
通过 pvesh / qm / pct 命令与 Proxmox VE 交互
"""

import subprocess
import json
from config import PVE_NODE


def _run_cmd(cmd_list, timeout=15):
    """执行命令并返回 (success, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, '', '命令执行超时'
    except FileNotFoundError:
        return False, '', f'命令未找到: {cmd_list[0]}'
    except Exception as e:
        return False, '', str(e)


def get_all_qemu_vms():
    """获取所有 KVM 虚拟机列表"""
    ok, stdout, stderr = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/qemu', '--output-format', 'json'
    ])
    if not ok:
        return []
    try:
        vms = json.loads(stdout)
        result = []
        for vm in vms:
            result.append({
                'vmid': vm.get('vmid'),
                'name': vm.get('name', ''),
                'status': vm.get('status', 'unknown'),
                'type': 'qemu'
            })
        return result
    except json.JSONDecodeError:
        return []


def get_all_lxc_vms():
    """获取所有 LXC 容器列表"""
    ok, stdout, stderr = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/lxc', '--output-format', 'json'
    ])
    if not ok:
        return []
    try:
        vms = json.loads(stdout)
        result = []
        for vm in vms:
            result.append({
                'vmid': vm.get('vmid'),
                'name': vm.get('name', ''),
                'status': vm.get('status', 'unknown'),
                'type': 'lxc'
            })
        return result
    except json.JSONDecodeError:
        return []


def get_all_vms():
    """获取所有虚拟机（KVM + LXC）"""
    vms = get_all_qemu_vms() + get_all_lxc_vms()
    vms.sort(key=lambda x: x['vmid'])
    return vms


def get_qemu_status(vmid):
    """获取 KVM 虚拟机当前状态（含 netin/netout）"""
    ok, stdout, stderr = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/qemu/{vmid}/status/current',
        '--output-format', 'json'
    ])
    if not ok:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def get_lxc_status(vmid):
    """获取 LXC 容器当前状态（含 netin/netout）"""
    ok, stdout, stderr = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/lxc/{vmid}/status/current',
        '--output-format', 'json'
    ])
    if not ok:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def get_vm_status(vmid, vm_type):
    """获取虚拟机当前状态"""
    if vm_type == 'qemu':
        return get_qemu_status(vmid)
    elif vm_type == 'lxc':
        return get_lxc_status(vmid)
    return None


def get_vm_network_traffic(vmid, vm_type):
    """
    获取虚拟机网络流量计数器
    返回: (netin_bytes, netout_bytes, status) 或 (None, None, None)
    netin/netout 是 PVE 启动以来的累计字节数
    """
    status = get_vm_status(vmid, vm_type)
    if status is None:
        return None, None, None

    vm_status = status.get('status', 'unknown')
    if vm_status != 'running':
        return 0, 0, vm_status

    netin = status.get('netin', 0)
    netout = status.get('netout', 0)
    return netin, netout, vm_status


def shutdown_qemu(vmid):
    """关闭 KVM 虚拟机"""
    ok, stdout, stderr = _run_cmd(['qm', 'shutdown', str(vmid)], timeout=30)
    return ok, stdout if ok else stderr


def shutdown_lxc(vmid):
    """关闭 LXC 容器"""
    ok, stdout, stderr = _run_cmd(['pct', 'shutdown', str(vmid)], timeout=30)
    return ok, stdout if ok else stderr


def shutdown_vm(vmid, vm_type):
    """关闭虚拟机"""
    if vm_type == 'qemu':
        return shutdown_qemu(vmid)
    elif vm_type == 'lxc':
        return shutdown_lxc(vmid)
    return False, f"未知的虚拟机类型: {vm_type}"


def get_vm_status_text(vmid, vm_type):
    """获取虚拟机运行状态文字"""
    status = get_vm_status(vmid, vm_type)
    if status is None:
        return 'unknown'
    return status.get('status', 'unknown')
