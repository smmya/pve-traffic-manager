#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PVE Traffic Manager Telegram Bot（python-telegram-bot 22.8）。"""

import asyncio
import datetime
import sys

import db
import monitor


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
        [InlineKeyboardButton('🔄 立即采集', callback_data='menu:collect')],
    ])


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


async def id_command(update, context):
    chat = update.effective_chat
    if chat:
        await update.effective_message.reply_text(f'当前会话 ID：{chat.id}')


async def start_command(update, context):
    if not await _require_authorized(update):
        return
    await update.effective_message.reply_text(
        'PVE Traffic Manager 已连接。可使用下方菜单或发送 /help 查看命令。',
        reply_markup=_menu_markup(),
    )


async def help_command(update, context):
    if not await _require_authorized(update):
        return
    await update.effective_message.reply_text(
        'PTM Bot 命令\n'
        '/status - 系统与预警摘要\n'
        '/traffic - 查看全部流量\n'
        '/group <组ID> - 查看一个组\n'
        '/vm <VMID> [qemu|lxc] - 查看单机流量\n'
        '/alerts - 查看达到预警线的机器\n'
        '/shutdowns - 最近超限关机记录\n'
        '/logs - 最近操作日志\n'
        '/collect - 立即采集一次（不会执行关机）\n'
        '/resetvm <VMID> <qemu|lxc> - 确认后重置该机器全部组流量\n'
        '/id - 查看当前会话 ID\n'
        '/menu - 显示快捷菜单',
        reply_markup=_menu_markup(),
    )


async def status_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_status_text(), reply_markup=_menu_markup())


async def traffic_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_traffic_text(), reply_markup=_menu_markup())


async def alerts_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_alerts_text(), reply_markup=_menu_markup())


async def logs_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_logs_text(), reply_markup=_menu_markup())


async def shutdowns_command(update, context):
    if await _require_authorized(update):
        await update.effective_message.reply_text(_logs_text('shutdown'), reply_markup=_menu_markup())


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
    group = db.get_group_by_id(group_id)
    if not group:
        await update.effective_message.reply_text('未找到该管理组。')
        return
    rows = db.get_group_traffic_overview(group_id)
    lines = [f"[{group_id}] {group['name']} · 单机限额 {_format_mb(group['traffic_limit_mb'])}"]
    for row in rows:
        used = row['total_in_mb'] + row['total_out_mb']
        percent = used * 100 / group['traffic_limit_mb']
        lines.append(
            f"{_type_label(row['vm_type'])} {row['vm_id']} {row.get('vm_name') or '-'}："
            f"{_format_mb(used)} ({percent:.1f}%)"
        )
    if not rows:
        lines.append('暂无虚拟机。')
    await update.effective_message.reply_text(_clip('\n'.join(lines)), reply_markup=_menu_markup())


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
    rows = db.get_vm_traffic_details(vm_id, vm_type)
    if not rows:
        await update.effective_message.reply_text('未找到该虚拟机的受管流量记录。')
        return
    lines = [f'虚拟机 {vm_id} 流量']
    for row in rows:
        used = row['total_in_mb'] + row['total_out_mb']
        percent = used * 100 / row['traffic_limit_mb']
        lines.append(
            f"{_type_label(row['vm_type'])} · {row['group_name']}："
            f"{_format_mb(used)} / {_format_mb(row['traffic_limit_mb'])} ({percent:.1f}%)\n"
            f"最近重置：{row['last_reset']}"
        )
    await update.effective_message.reply_text(_clip('\n\n'.join(lines)), reply_markup=_menu_markup())


async def collect_command(update, context):
    if not await _require_authorized(update):
        return
    message = update.effective_message
    await message.reply_text('正在执行一次仅采集任务，不会触发关机…')
    ok = await asyncio.to_thread(monitor.run_monitor, False, True)
    await message.reply_text(
        ('采集完成。' if ok else '已有监控任务在运行，本次已跳过。') + '\n\n' + _status_text(),
        reply_markup=_menu_markup(),
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
        InlineKeyboardButton('确认重置', callback_data=f'reset:{vm_type}:{vm_id}'),
        InlineKeyboardButton('取消', callback_data='reset:cancel'),
    ]])
    await update.effective_message.reply_text(
        f'确认重置 {_type_label(vm_type)} {vm_id} 在全部管理组中的流量？\n此操作不影响同组其他机器。',
        reply_markup=keyboard,
    )


async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    if not _is_authorized(update):
        await query.edit_message_text('此会话未获 PTM 授权。')
        return
    data = query.data or ''
    if data == 'menu:status':
        text = _status_text()
    elif data == 'menu:traffic':
        text = _traffic_text()
    elif data == 'menu:alerts':
        text = _alerts_text()
    elif data == 'menu:logs':
        text = _logs_text()
    elif data == 'menu:collect':
        await query.edit_message_text('正在执行一次仅采集任务，不会触发关机…')
        ok = await asyncio.to_thread(monitor.run_monitor, False, True)
        text = ('采集完成。' if ok else '已有监控任务在运行，本次已跳过。') + '\n\n' + _status_text()
    elif data == 'reset:cancel':
        await query.edit_message_text('已取消重置。', reply_markup=_menu_markup())
        return
    elif data.startswith('reset:'):
        try:
            _, vm_type, raw_vm_id = data.split(':', 2)
            vm_id = int(raw_vm_id)
        except (ValueError, TypeError):
            await query.edit_message_text('重置请求格式无效。')
            return
        if vm_type not in ('qemu', 'lxc'):
            await query.edit_message_text('重置请求类型无效。')
            return
        groups = db.reset_vm_traffic_all_groups(
            vm_id, vm_type,
            f'Telegram 会话 {update.effective_chat.id} 手动重置全部组流量',
        )
        text = (
            f'已重置 {_type_label(vm_type)} {vm_id}，涉及 {len(groups)} 个组；'
            '同组其他机器未受影响。'
            if groups else '该机器已不在任何管理组中。'
        )
    else:
        text = '未知操作。'
    await query.edit_message_text(_clip(text), reply_markup=_menu_markup())


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
    await application.bot.set_my_commands([
        BotCommand('menu', '显示 PTM 快捷菜单'),
        BotCommand('status', '系统与预警摘要'),
        BotCommand('traffic', '查看全部流量'),
        BotCommand('alerts', '查看预警机器'),
        BotCommand('collect', '立即执行仅采集'),
        BotCommand('logs', '查看操作日志'),
        BotCommand('help', '查看命令帮助'),
        BotCommand('id', '查看当前会话 ID'),
    ])


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
    application.add_handler(CommandHandler('resetvm', resetvm_command))
    application.add_handler(CallbackQueryHandler(callback_handler, pattern=r'^(menu|reset):'))
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
