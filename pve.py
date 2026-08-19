# -*- coding: utf-8 -*-
"""
PVE 流量控制管理器 - PVE API 交互封装
通过 pvesh / qm / pct 命令与 Proxmox VE 交互
"""

import os
import subprocess
import json
import time
import ipaddress
from config import PVE_NODE


def _run_cmd(cmd_list, timeout=15):
    """执行命令并返回 (success, stdout, stderr)"""
    # 确保 /usr/sbin 在 PATH 中（cron 环境下缺失）
    env = os.environ.copy()
    env['PATH'] = '/usr/sbin:/usr/bin:/bin' + (':' + env.get('PATH', '') if env.get('PATH') else '')
    try:
        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, '', '命令执行超时'
    except FileNotFoundError:
        return False, '', f'命令未找到: {cmd_list[0]}'
    except Exception as e:
        return False, '', str(e)


def get_vm_config_name(vmid, vm_type):
    """从 VM 配置读取名称，兼容 LXC 列表不返回 name 的 PVE 版本。"""
    if vm_type not in ('qemu', 'lxc'):
        return ''
    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return ''
    ok, stdout, _ = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/{vm_type}/{vmid}/config',
        '--output-format', 'json'
    ])
    if not ok:
        return ''
    try:
        config = json.loads(stdout)
    except json.JSONDecodeError:
        return ''
    if not isinstance(config, dict):
        return ''
    keys = ('hostname', 'name') if vm_type == 'lxc' else ('name',)
    for key in keys:
        value = str(config.get(key) or '').strip()
        if value:
            return value
    return ''


def get_all_qemu_vms():
    """获取所有 KVM 虚拟机列表"""
    ok, stdout, stderr = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/qemu', '--output-format', 'json'
    ])
    if not ok:
        return []
    try:
        vms = json.loads(stdout)
        if not isinstance(vms, list):
            return []
        result = []
        for vm in vms:
            try:
                vmid = int(vm['vmid'])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(vm.get('name') or '').strip()
            if not name:
                name = get_vm_config_name(vmid, 'qemu')
            result.append({
                'vmid': vmid,
                'name': name,
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
        if not isinstance(vms, list):
            return []
        result = []
        for vm in vms:
            try:
                vmid = int(vm['vmid'])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(vm.get('name') or vm.get('hostname') or '').strip()
            if not name:
                name = get_vm_config_name(vmid, 'lxc')
            result.append({
                'vmid': vmid,
                'name': name,
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


def ping_from_lxc(vmid, target, count=3, timeout_seconds=3):
    """通过 pct exec 在 LXC 网络命名空间内执行 ping。"""
    try:
        vmid = int(vmid)
        address = ipaddress.ip_address(str(target).strip())
        count = int(count)
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        return False, '无效的容器 ID、检测 IP 或 ping 参数'
    if vmid <= 0 or count != 3 or timeout_seconds < 1:
        return False, '网络检测必须发送 3 个 ping 请求'

    ping_command = ['ping']
    if address.version == 6:
        ping_command.append('-6')
    ping_command.extend([
        '-c', str(count), '-W', str(timeout_seconds), str(address),
    ])
    ok, stdout, stderr = _run_cmd(
        ['pct', 'exec', str(vmid), '--', *ping_command],
        timeout=count * timeout_seconds + 15,
    )
    detail = stdout if ok else (stderr or stdout or '3 次 ping 均未收到响应')
    return ok, detail[-1000:]


def get_qemu_status(vmid):
    """获取 KVM 虚拟机当前状态（含 netin/netout）"""
    ok, stdout, stderr = _run_cmd([
        'pvesh', 'get', f'/nodes/{PVE_NODE}/qemu/{vmid}/status/current',
        '--output-format', 'json'
    ])
    if not ok:
        return None
    try:
        status = json.loads(stdout)
        return status if isinstance(status, dict) else None
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
        status = json.loads(stdout)
        return status if isinstance(status, dict) else None
    except json.JSONDecodeError:
        return None


def get_vm_status(vmid, vm_type):
    """获取虚拟机当前状态"""
    if vm_type == 'qemu':
        return get_qemu_status(vmid)
    elif vm_type == 'lxc':
        return get_lxc_status(vmid)
    return None


def get_vm_boot_time(status, now=None):
    """根据 PVE uptime 估算本次启动时间，用于识别计数器重启。"""
    if not status:
        return None
    try:
        uptime = max(0, int(status.get('uptime')))
    except (TypeError, ValueError):
        return None
    return int(time.time() if now is None else now) - uptime


def get_vm_network_snapshot(vmid, vm_type):
    """
    获取虚拟机网络流量计数器和启动时间。
    返回: (netin_bytes, netout_bytes, status, boot_time)
    netin/netout 是 PVE 启动以来的累计字节数
    """
    status = get_vm_status(vmid, vm_type)
    if status is None:
        return None, None, None, None

    vm_status = status.get('status', 'unknown')
    boot_time = get_vm_boot_time(status)
    if vm_status != 'running':
        return 0, 0, vm_status, boot_time

    try:
        netin = int(status['netin'])
        netout = int(status['netout'])
    except (TypeError, ValueError):
        return None, None, None, boot_time
    except KeyError:
        return None, None, None, boot_time
    if netin < 0 or netout < 0:
        return None, None, None, boot_time
    return netin, netout, vm_status, boot_time


def get_vm_network_traffic(vmid, vm_type):
    """兼容旧调用方，返回 (netin_bytes, netout_bytes, status)。"""
    netin, netout, status, _ = get_vm_network_snapshot(vmid, vm_type)
    return netin, netout, status


def shutdown_qemu(vmid):
    """关闭 KVM 虚拟机"""
    # 明确 PVE 等待上限，并给本地 subprocess 留出收尾余量。
    ok, stdout, stderr = _run_cmd(
        ['qm', 'shutdown', str(vmid), '--timeout', '60'], timeout=75
    )
    return ok, stdout if ok else stderr


def shutdown_lxc(vmid):
    """关闭 LXC 容器"""
    ok, stdout, stderr = _run_cmd(
        ['pct', 'shutdown', str(vmid), '--timeout', '60'], timeout=75
    )
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
