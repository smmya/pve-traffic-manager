#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PVE Traffic Manager Telegram Bot（python-telegram-bot 22.8）。"""

import asyncio
import datetime
import random
import sys

import db
import monitor
import pve


NETWORK_JOB_SECONDS = 60
NETWORK_CONTAINER_DELAY_SECONDS = 30
BOT_COMMAND_DEFINITIONS = (
    ('menu', '显示 PTM 可视化菜单'),
    ('help', '查看按钮操作说明'),
    ('id', '查看当前会话 ID'),
)


def _type_label(vm_type):
    return 'KVM' if vm_type == 'qemu' else 'LXC'


def _format_mb(value):
    value = float(value or 0)
    if value >= 1024:
        return f'{value / 1024:.2f} GB'
    return f'{value:.2f} MB'


def _clip(text, limit=4000):
    if len(text) <= limit:
        return text
    return text[:limit - 20] + '\n…（内容已截断）'


def _settings():
    return db.get_telegram_settings()


def _is_authorized(update):
    chat = update.effective_chat
    configured = _settings().get('chat_id', '').strip()
    return bool(chat and configured and str(chat.id) == configured)


async def _require_authorized(update):
    if _is_authorized(update):
        return True
    message = update.effective_message
    if message:
        await message.reply_text('此会话未获 PTM 授权。发送 /id 查看当前会话 ID。')
    return False


def _menu_markup():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('📊 状态', callback_data='menu:status'),
            InlineKeyboardButton('📈 流量', callback_data='menu:traffic'),
        ],
        [
            InlineKeyboardButton('⚠️ 预警', callback_data='menu:alerts'),
            InlineKeyboardButton('🧾 日志', callback_data='menu:logs'),
        ],
        [
            InlineKeyboardButton('♻️ 重置数据', callback_data='reset:menu'),
            InlineKeyboardButton('🌐 网络检测', callback_data='network:status'),
        ],
    ])


def _help_text():
    return (
        'PTM Bot 按钮操作说明\n'
        '请直接点击下方菜单，无需输入管理参数或记忆命令。\n\n'
        '📊 状态：查看系统与预警摘要\n'
        '📈 流量：查看管理组及机器流量\n'
        '⚠️ 预警：查看达到预警线的机器\n'
        '🧾 日志：查看操作与超限关机记录\n'
        '♻️ 重置数据：选择按组或按机器重置，再选择对象并二次确认\n'
        '🌐 网络检测：查看状态或手动执行检测\n\n'
        '每个子页面均有返回按钮。'
    )


def _back_markup(parent='menu:main', extra_rows=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton('⬅️ 返回', callback_data=parent)])
    return InlineKeyboardMarkup(rows)


def _status_markup():
    from telegram import InlineKeyboardButton
    return _back_markup(extra_rows=[[
        InlineKeyboardButton('🔄 刷新状态', callback_data='menu:status'),
    ]])


def _alerts_markup():
    from telegram import InlineKeyboardButton
    return _back_markup(extra_rows=[[
        InlineKeyboardButton('🔄 刷新预警', callback_data='menu:alerts'),
    ]])


def _logs_markup():
    from telegram import InlineKeyboardButton
    return _back_markup(extra_rows=[[
        InlineKeyboardButton('🛑 关机记录', callback_data='view:shutdowns'),
    ]])


def _traffic_markup():
    from telegram import InlineKeyboardButton
    buttons = [
        InlineKeyboardButton(
            f"[{group['id']}] {group['name'][:18]}",
            callback_data=f"view:group:{group['id']}",
        )
        for group in db.get_all_groups()[:20]
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    return _back_markup(extra_rows=rows)


def _group_markup(group_id):
    from telegram import InlineKeyboardButton
    return _back_markup('menu:traffic', [[
        InlineKeyboardButton('♻️ 重置本组', callback_data=f'resetgroup:ask:{group_id}'),
    ]])


def _vm_markup(vm_id, vm_type):
    from telegram import InlineKeyboardButton
    return _back_markup('menu:traffic', [[
        InlineKeyboardButton(
            '♻️ 重置此机器', callback_data=f'resetvm:ask:{vm_type}:{vm_id}'
        ),
    ]])


def _reset_menu_markup():
    from telegram import InlineKeyboardButton
    return _back_markup(extra_rows=[[
        InlineKeyboardButton('按组重置', callback_data='reset:groups'),
        InlineKeyboardButton('按机器重置', callback_data='reset:vms'),
    ]])


def _reset_groups_markup(page=0):
    from telegram import InlineKeyboardButton
    groups = db.get_all_groups()
    page_size = 12
    max_page = max(0, (len(groups) - 1) // page_size)
    page = min(max(0, int(page)), max_page)
    visible = groups[page * page_size:(page + 1) * page_size]
    buttons = [
        InlineKeyboardButton(
            f"[{group['id']}] {group['name'][:18]}",
            callback_data=f"resetgroup:ask:{group['id']}",
        )
        for group in visible
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton('⬅️ 上一页', callback_data=f'reset:groups:{page - 1}'))
    if page < max_page:
        navigation.append(InlineKeyboardButton('下一页 ➡️', callback_data=f'reset:groups:{page + 1}'))
    if navigation:
        rows.append(navigation)
    return _back_markup('reset:menu', rows)


def _reset_vms_markup(page=0):
    from telegram import InlineKeyboardButton
    vms = db.get_all_managed_vms()
    page_size = 12
    max_page = max(0, (len(vms) - 1) // page_size)
    page = min(max(0, int(page)), max_page)
    visible = vms[page * page_size:(page + 1) * page_size]
    buttons = [
        InlineKeyboardButton(
            f"{_type_label(vm['vm_type'])} {vm['vm_id']} {(vm.get('vm_name') or '')[:12]}",
            callback_data=f"resetvm:ask:{vm['vm_type']}:{vm['vm_id']}",
        )
        for vm in visible
    ]
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton('⬅️ 上一页', callback_data=f'reset:vms:{page - 1}'))
    if page < max_page:
        navigation.append(InlineKeyboardButton('下一页 ➡️', callback_data=f'reset:vms:{page + 1}'))
    if navigation:
        rows.append(navigation)
    return _back_markup('reset:menu', rows)


def _network_markup():
    from telegram import InlineKeyboardButton
    return _back_markup(extra_rows=[[
        InlineKeyboardButton('🔄 刷新', callback_data='network:status'),
        InlineKeyboardButton('▶️ 立即检测', callback_data='network:run'),
    ]])


def _status_text():
    groups = db.get_all_groups()
    vms = db.get_all_managed_vms()
    warning_percent = _settings().get('warning_percent', 80)
    alerts = db.get_usage_alerts(warning_percent)
    shutdowns = sum(1 for item in alerts if item['usage_percent'] >= 100)
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return (
        'PTM 运行状态\n'
        f'时间：{now}\n'
        f'管理组：{len(groups)}\n'
        f'管理虚拟机：{len(vms)}\n'
        f'≥{warning_percent:g}%：{len(alerts)}\n'
        f'≥100%：{shutdowns}\n'
        f'Telegram 推送：{"已启用" if _settings().get("enabled") else "未启用"}'
    )


def _traffic_text():
    groups = db.get_all_groups()
    if not groups:
        return '尚未创建管理组。'
    lines = ['各组流量（限额按每台虚拟机计算）']
    for group in groups:
        rows = db.get_group_traffic_overview(group['id'])
        lines.append(f"\n[{group['id']}] {group['name']} · 单机限额 {_format_mb(group['traffic_limit_mb'])}")
        if not rows:
            lines.append('  暂无虚拟机')
            continue
        for row in rows:
            used = row['total_in_mb'] + row['total_out_mb']
            percent = used * 100 / group['traffic_limit_mb']
            lines.append(
                f"  {_type_label(row['vm_type'])} {row['vm_id']} {row.get('vm_name') or '-'}："
                f"{_format_mb(used)} ({percent:.1f}%)"
            )
    return _clip('\n'.join(lines))


def _group_text(group_id):
    group = db.get_group_by_id(group_id)
    if not group:
        return None
    rows = db.get_group_traffic_overview(group_id)
    lines = [
        f"[{group_id}] {group['name']} · 单机限额 {_format_mb(group['traffic_limit_mb'])}"
    ]
    for row in rows:
        used = row['total_in_mb'] + row['total_out_mb']
        percent = used * 100 / group['traffic_limit_mb']
        lines.append(
            f"{_type_label(row['vm_type'])} {row['vm_id']} {row.get('vm_name') or '-'}："
            f"{_format_mb(used)} ({percent:.1f}%)"
        )
    if not rows:
        lines.append('暂无虚拟机。')
    return _clip('\n'.join(lines))


def _vm_text(vm_id, vm_type=None):
    rows = db.get_vm_traffic_details(vm_id, vm_type)
    if not rows:
        return None
    lines = [f'虚拟机 {vm_id} 流量']
    for row in rows:
        used = row['total_in_mb'] + row['total_out_mb']
        percent = used * 100 / row['traffic_limit_mb']
        lines.append(
            f"{_type_label(row['vm_type'])} · {row['group_name']}："
            f"{_format_mb(used)} / {_format_mb(row['traffic_limit_mb'])} ({percent:.1f}%)\n"
            f"最近重置：{row['last_reset']}"
        )
    return _clip('\n\n'.join(lines))


def _alerts_text():
    percent = _settings().get('warning_percent', 80)
    rows = db.get_usage_alerts(percent)
    if not rows:
        return f'当前没有达到 {percent:g}% 的虚拟机。'
    lines = [f'流量预警（≥{percent:g}%）']
    for row in rows:
        used = row['total_in_mb'] + row['total_out_mb']
        marker = '🛑' if row['usage_percent'] >= 100 else '⚠️'
        lines.append(
            f"{marker} {_type_label(row['vm_type'])} {row['vm_id']} "
            f"{row.get('vm_name') or '-'} · {row['group_name']}\n"
            f"   {_format_mb(used)} / {_format_mb(row['traffic_limit_mb'])} "
            f"({row['usage_percent']:.1f}%)"
        )
    return _clip('\n'.join(lines))


def _logs_text(action=None):
    logs = db.get_action_logs(limit=100)
    if action:
        logs = [item for item in logs if item['action'] == action]
    logs = logs[:10]
    if not logs:
        return '暂无相关操作日志。'
    lines = ['最近操作日志']
    for item in logs:
        detail = (item.get('detail') or '').replace('\n', ' ')
        lines.append(f"{item['created_at']} · {item['action']}\n{detail[:180]}")
    return _clip('\n\n'.join(lines))


def _network_status_text():
    settings = db.get_network_check_settings()
    targets = settings.get('targets') or '[未设置]'
    return (
        'LXC 网络状态检测\n'
        f"状态：{'已启用' if settings.get('enabled') else '未启用'}\n"
        f"检测 IP：{targets}\n"
        f"检测周期：{float(settings.get('interval_hours', 6)):g} 小时\n"
        '范围：全部正在运行的 LXC\n'
        f'容器间隔：{NETWORK_CONTAINER_DELAY_SECONDS} 秒\n'
        '单次规则：随机选择一个 IP，发送 3 个 ping\n'
        f"上次开始：{settings.get('last_started_at') or '-'}\n"
        f"上次完成：{settings.get('last_completed_at') or '-'}\n"
        f"上次结果：{settings.get('last_result') or '-'}"
    )


async def run_network_check_cycle(bot, force=False):
    """顺序检测全部运行中 LXC；自动检测仅在失败时发送 Telegram。"""
    claimed, claim_result = db.try_claim_network_check(force=force)
    if not claimed:
        return {'started': False, 'detail': claim_result}

    checked = 0
    failed = 0
    notification_errors = 0
    try:
        containers = await asyncio.to_thread(pve.get_all_lxc_vms)
        running = [item for item in containers if item.get('status') == 'running']
        targets = claim_result['target_list']
        chat_id = db.get_telegram_settings().get('chat_id', '').strip()

        for index, container in enumerate(running):
            target = random.choice(targets)
            ok, detail = await asyncio.to_thread(
                pve.ping_from_lxc, container['vmid'], target, 3, 3
            )
            checked += 1
            db.record_network_check(
                container['vmid'], container.get('name', ''), target, ok, detail
            )
            if not ok:
                failed += 1
                text = (
                    '🌐 PTM LXC 网络异常\n'
                    f"容器：LXC {container['vmid']} '{container.get('name') or '-'}'\n"
                    f'检测 IP：{target}\n'
                    '结果：3 次 ping 均未成功\n'
                    f'详情：{str(detail)[:500]}'
                )
                if chat_id:
                    try:
                        await bot.send_message(chat_id=int(chat_id), text=text)
                    except Exception as exc:
                        notification_errors += 1
                        token = db.get_telegram_settings().get('bot_token', '')
                        message = str(exc).replace(token, '[已隐藏 Token]')[:300]
                        db.insert_action_log(
                            'telegram_error', target_type='vm',
                            target_id=container['vmid'], detail=message,
                        )
            if index < len(running) - 1:
                await asyncio.sleep(NETWORK_CONTAINER_DELAY_SECONDS)

        result = f'检测 {checked} 个运行中 LXC，异常 {failed} 个'
        if notification_errors:
            result += f'，通知失败 {notification_errors} 个'
        db.finish_network_check(result)
        return {
            'started': True, 'checked': checked, 'failed': failed,
            'notification_errors': notification_errors, 'detail': result,
        }
    except Exception as exc:
        token = db.get_telegram_settings().get('bot_token', '')
        message = str(exc).replace(token, '[已隐藏 Token]')[:300]
        db.finish_network_check(f'检测异常: {message}')
        db.insert_action_log(
            'network_check_error', target_type='system', detail=message
        )
        return {'started': True, 'checked': checked, 'failed': failed,
                'detail': f'检测异常: {message}'}


async def network_monitor_job(context):
    await run_network_check_cycle(context.bot, force=False)


async def id_command(update, context):
    chat = update.effective_chat
    if chat:
        await update.effective_message.reply_text(f'当前会话 ID：{chat.id}')


async def start_command(update, context):
    if not await _require_authorized(update):
        return
    await update.effective_message.reply_text(
        'PVE Traffic Manager 已连接。请直接点击下方按钮操作，无需输入管理命令。',
        reply_markup=_menu_markup(),
    )


async def help_command(update, context):
    if not await _require_authorized(update):
        return
    await update.effective_message.reply_text(
        _help_text(),
        reply_markup=_menu_markup(),
    )


async def status_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_status_text(), reply_markup=_status_markup())


async def traffic_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_traffic_text(), reply_markup=_traffic_markup())


async def alerts_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_alerts_text(), reply_markup=_alerts_markup())


async def logs_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_logs_text(), reply_markup=_logs_markup())


async def shutdowns_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(
            _logs_text('shutdown'), reply_markup=_back_markup('menu:logs')
        )


async def group_command(update, context):
    if not await _require_authorized(update):
        return
    if not context.args:
        await update.effective_message.reply_text('用法：/group <组ID>')
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text('组 ID 必须是整数。')
        return
    text = _group_text(group_id)
    if text is None:
        await update.effective_message.reply_text('未找到该管理组。')
        return
    await update.effective_message.reply_text(text, reply_markup=_group_markup(group_id))


async def vm_command(update, context):
    if not await _require_authorized(update):
        return
    if not context.args:
        await update.effective_message.reply_text('用法：/vm <VMID> [qemu|lxc]')
        return
    try:
        vm_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text('VMID 必须是整数。')
        return
    vm_type = context.args[1].lower() if len(context.args) > 1 else None
    if vm_type not in (None, 'qemu', 'lxc'):
        await update.effective_message.reply_text('类型只能是 qemu 或 lxc。')
        return
    details = db.get_vm_traffic_details(vm_id, vm_type)
    text = _vm_text(vm_id, vm_type)
    if text is None:
        await update.effective_message.reply_text('未找到该虚拟机的受管流量记录。')
        return
    vm_types = {item['vm_type'] for item in details}
    markup = (
        _vm_markup(vm_id, next(iter(vm_types)))
        if len(vm_types) == 1 else _back_markup('menu:traffic')
    )
    await update.effective_message.reply_text(
        text, reply_markup=markup
    )


async def collect_command(update, context):
    if not await _require_authorized(update):
        return
    message = update.effective_message
    await message.reply_text('正在执行一次仅采集任务，不会触发关机…')
    ok = await asyncio.to_thread(monitor.run_monitor, False, True)
    await message.reply_text(
        ('采集完成。' if ok else '已有监控任务在运行，本次已跳过。') + '\n\n' + _status_text(),
        reply_markup=_status_markup(),
    )


async def resetgroup_command(update, context):
    if not await _require_authorized(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text('用法：/resetgroup <组ID>')
        return
    try:
        group_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text('组 ID 必须是整数。')
        return
    group = db.get_group_by_id(group_id)
    if not group:
        await update.effective_message.reply_text('未找到该管理组。')
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            '确认重置整组', callback_data=f'resetgroup:confirm:{group_id}'
        ),
        InlineKeyboardButton('取消', callback_data='reset:groups'),
    ]])
    await update.effective_message.reply_text(
        f"确认重置管理组 [{group_id}] {group['name']} 内所有机器的流量？",
        reply_markup=keyboard,
    )


async def resetvm_command(update, context):
    if not await _require_authorized(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text('用法：/resetvm <VMID> <qemu|lxc>')
        return
    try:
        vm_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text('VMID 必须是整数。')
        return
    vm_type = context.args[1].lower()
    if vm_type not in ('qemu', 'lxc'):
        await update.effective_message.reply_text('类型只能是 qemu 或 lxc。')
        return
    if not db.get_vm_traffic_details(vm_id, vm_type):
        await update.effective_message.reply_text('未找到该虚拟机的受管流量记录。')
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            '确认重置此机器', callback_data=f'resetvm:confirm:{vm_type}:{vm_id}'
        ),
        InlineKeyboardButton('取消', callback_data='reset:vms'),
    ]])
    await update.effective_message.reply_text(
        f'确认重置 {_type_label(vm_type)} {vm_id} 在全部管理组中的流量？\n此操作不影响同组其他机器。',
        reply_markup=keyboard,
    )


async def netcheck_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(
            _network_status_text(), reply_markup=_network_markup()
        )


async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    if not _is_authorized(update):
        await query.edit_message_text('此会话未获 PTM 授权。')
        return
    data = query.data or ''
    if data == 'menu:main':
        text = 'PVE Traffic Manager 主菜单'
        markup = _menu_markup()
    elif data == 'menu:status':
        text, markup = _status_text(), _status_markup()
    elif data == 'menu:traffic':
        text, markup = _traffic_text(), _traffic_markup()
    elif data == 'menu:alerts':
        text, markup = _alerts_text(), _alerts_markup()
    elif data == 'menu:logs':
        text, markup = _logs_text(), _logs_markup()
    elif data == 'menu:collect':
        text = '“立即采集”入口已移除，请返回主菜单选择其他功能。'
        markup = _back_markup()
    elif data == 'view:shutdowns':
        text, markup = _logs_text('shutdown'), _back_markup('menu:logs')
    elif data.startswith('view:group:'):
        try:
            group_id = int(data.rsplit(':', 1)[1])
        except ValueError:
            group_id = -1
        text = _group_text(group_id) or '未找到该管理组。'
        markup = _group_markup(group_id) if db.get_group_by_id(group_id) else _back_markup('menu:traffic')
    elif data == 'reset:menu':
        text, markup = '请选择数据重置方式。所有重置操作都需要再次确认。', _reset_menu_markup()
    elif data.startswith('reset:groups'):
        try:
            page = int(data.split(':', 2)[2]) if data.count(':') == 2 else 0
        except ValueError:
            page = 0
        text, markup = '请选择要重置的管理组。', _reset_groups_markup(page)
    elif data.startswith('reset:vms'):
        try:
            page = int(data.split(':', 2)[2]) if data.count(':') == 2 else 0
        except ValueError:
            page = 0
        text, markup = '请选择要重置的机器。机器会在其所属全部组中重置。', _reset_vms_markup(page)
    elif data.startswith('resetgroup:ask:'):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        try:
            group_id = int(data.rsplit(':', 1)[1])
        except ValueError:
            group_id = -1
        group = db.get_group_by_id(group_id)
        if not group:
            text, markup = '未找到该管理组。', _back_markup('reset:groups')
        else:
            text = f"确认重置管理组 [{group_id}] {group['name']} 内所有机器的流量？"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    '确认重置整组', callback_data=f'resetgroup:confirm:{group_id}'
                ),
                InlineKeyboardButton('取消', callback_data='reset:groups'),
            ]])
    elif data.startswith('resetgroup:confirm:'):
        try:
            group_id = int(data.rsplit(':', 1)[1])
        except ValueError:
            group_id = -1
        group = db.get_group_by_id(group_id)
        if group:
            db.reset_group_traffic(group_id)
            db.insert_action_log(
                'reset', target_type='group', target_id=group_id,
                detail=f"Telegram 会话 {update.effective_chat.id} 重置组 {group['name']}",
            )
            text = f"已重置管理组 [{group_id}] {group['name']} 内全部机器的流量。"
        else:
            text = '该管理组已不存在。'
        markup = _back_markup('reset:menu')
    elif data.startswith('resetvm:ask:'):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        try:
            _, _, vm_type, raw_vm_id = data.split(':', 3)
            vm_id = int(raw_vm_id)
        except (ValueError, TypeError):
            vm_type, vm_id = '', -1
        if vm_type not in ('qemu', 'lxc') or not db.get_vm_traffic_details(vm_id, vm_type):
            text, markup = '未找到该机器的受管流量记录。', _back_markup('reset:vms')
        else:
            text = (
                f'确认重置 {_type_label(vm_type)} {vm_id} 在全部管理组中的流量？\n'
                '此操作不影响同组其他机器。'
            )
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    '确认重置此机器',
                    callback_data=f'resetvm:confirm:{vm_type}:{vm_id}',
                ),
                InlineKeyboardButton('取消', callback_data='reset:vms'),
            ]])
    elif data.startswith('resetvm:confirm:'):
        try:
            _, _, vm_type, raw_vm_id = data.split(':', 3)
            vm_id = int(raw_vm_id)
        except (ValueError, TypeError):
            vm_type, vm_id = '', -1
        groups = (
            db.reset_vm_traffic_all_groups(
                vm_id, vm_type,
                f'Telegram 会话 {update.effective_chat.id} 手动重置全部组流量',
            )
            if vm_type in ('qemu', 'lxc') else []
        )
        text = (
            f'已重置 {_type_label(vm_type)} {vm_id}，涉及 {len(groups)} 个组；'
            '同组其他机器未受影响。'
            if groups else '该机器已不在任何管理组中。'
        )
        markup = _back_markup('reset:menu')
    elif data == 'network:status':
        text, markup = _network_status_text(), _network_markup()
    elif data == 'network:run':
        await query.edit_message_text(
            '正在通过各运行中 LXC 依次检测网络；容器之间间隔 30 秒，请稍候…',
            reply_markup=_back_markup(),
        )
        result = await run_network_check_cycle(context.bot, force=True)
        text = f"{result['detail']}\n\n{_network_status_text()}"
        markup = _network_markup()
    else:
        text, markup = '未知操作。', _back_markup()
    await query.edit_message_text(_clip(text), reply_markup=markup)


async def unknown_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text('未知命令。发送 /help 查看可用功能。')


async def error_handler(update, context):
    token = _settings().get('bot_token', '')
    message = str(context.error)
    if token:
        message = message.replace(token, '[已隐藏 Token]')
    message = message.replace('\n', ' ')[:300]
    db.insert_action_log('telegram_error', target_type='system', detail=message)
    print(f'[Telegram Bot 错误] {message}', file=sys.stderr)


async def post_init(application):
    from telegram import BotCommand
    db.recover_interrupted_network_check()
    await application.bot.set_my_commands([
        BotCommand(command, description)
        for command, description in BOT_COMMAND_DEFINITIONS
    ])
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            network_monitor_job,
            interval=NETWORK_JOB_SECONDS,
            first=10,
            name='ptm-lxc-network-monitor',
            job_kwargs={'max_instances': 1, 'coalesce': True},
        )
    elif db.get_network_check_settings().get('enabled'):
        db.insert_action_log(
            'network_check_error', target_type='system',
            detail='缺少 PTB job-queue 依赖，网络检测未启动',
        )


def build_application(token):
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(False)
        .post_init(post_init)
        .build()
    )
    application.add_handler(CommandHandler(['start', 'menu'], start_command))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('id', id_command))
    application.add_handler(CommandHandler('status', status_command))
    application.add_handler(CommandHandler('traffic', traffic_command))
    application.add_handler(CommandHandler('group', group_command))
    application.add_handler(CommandHandler('vm', vm_command))
    application.add_handler(CommandHandler('alerts', alerts_command))
    application.add_handler(CommandHandler('shutdowns', shutdowns_command))
    application.add_handler(CommandHandler('logs', logs_command))
    application.add_handler(CommandHandler('collect', collect_command))
    application.add_handler(CommandHandler('resetgroup', resetgroup_command))
    application.add_handler(CommandHandler('resetvm', resetvm_command))
    application.add_handler(CommandHandler('netcheck', netcheck_command))
    application.add_handler(CallbackQueryHandler(
        callback_handler,
        pattern=r'^(menu|view|reset|resetgroup|resetvm|network):',
    ))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)
    return application


def main():
    db.init_db()
    settings = db.get_telegram_settings()
    token = settings.get('bot_token', '').strip()
    chat_id = settings.get('chat_id', '').strip()
    if not token:
        print('Telegram Bot 未启动：请先在 PTM 系统设置中配置 Token。')
        return 2
    try:
        application = build_application(token)
    except ImportError:
        print('Telegram Bot 未启动：缺少 python-telegram-bot，请安装 requirements.txt。')
        return 2
    except Exception as exc:
        message = str(exc).replace(token, '[已隐藏 Token]')
        print(f'Telegram Bot 初始化失败：{message}')
        return 1

    print(f"PTM Telegram Bot 已启动，授权会话 ID：{chat_id or '[未设置，可发送 /id 查询]'}")
    try:
        application.run_polling(drop_pending_updates=False)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        message = str(exc).replace(token, '[已隐藏 Token]')
        print(f'Telegram Bot 运行失败：{message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
