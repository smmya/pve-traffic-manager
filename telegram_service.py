#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PTM Telegram 通知、诊断和 systemd 服务管理。"""

import asyncio
import importlib.metadata
import os
import subprocess
import tempfile
import urllib.error
import urllib.request

import db
from config import (
    BASE_DIR,
    PYTHON_PATH,
    PYTHON_PACKAGES_DIR,
    TELEGRAM_BOT_SCRIPT,
    TELEGRAM_SERVICE_NAME,
    TELEGRAM_SERVICE_PATH,
)


REQUIREMENTS_PATH = os.path.join(BASE_DIR, 'requirements.txt')
TELEGRAM_API_URL = 'https://api.telegram.org/'


def mask_token(token):
    """只展示 Token 的首尾片段，避免 CLI/日志泄露完整密钥。"""
    token = str(token or '').strip()
    if not token:
        return '[未设置]'
    if len(token) <= 10:
        return token[:2] + '***' + token[-2:]
    return token[:6] + '***' + token[-4:]


def dependency_status():
    try:
        version = importlib.metadata.version('python-telegram-bot')
        return True, version
    except importlib.metadata.PackageNotFoundError:
        return False, '未安装'


def check_network(timeout=5):
    """独立检查到 Telegram API 的 HTTPS 可达性，不携带 Bot Token。"""
    request = urllib.request.Request(
        TELEGRAM_API_URL,
        headers={'User-Agent': 'pve-traffic-manager/telegram-check'},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f'HTTPS 可达 (HTTP {response.status})'
    except urllib.error.HTTPError as exc:
        # 收到 HTTP 响应已经证明 DNS/TCP/TLS 链路可达。
        return True, f'HTTPS 可达 (HTTP {exc.code})'
    except urllib.error.URLError as exc:
        return False, f'无法连接: {exc.reason}'
    except Exception as exc:
        return False, f'无法连接: {exc}'


def _load_bot_class():
    from telegram import Bot
    return Bot


def _safe_error(exc, token=''):
    message = str(exc) or exc.__class__.__name__
    if token:
        message = message.replace(token, '[已隐藏 Token]')
    return message.replace('\n', ' ')[:300]


async def _probe_bot(bot_token, chat_id):
    Bot = _load_bot_class()
    async with Bot(bot_token) as bot:
        me = await bot.get_me(
            connect_timeout=5, read_timeout=5, write_timeout=5, pool_timeout=5
        )
        chat_result = None
        chat_error = None
        if chat_id:
            try:
                chat_result = await bot.get_chat(
                    int(chat_id), connect_timeout=5, read_timeout=5,
                    write_timeout=5, pool_timeout=5,
                )
            except Exception as exc:  # Token 已验证，聊天错误应单独报告。
                chat_error = _safe_error(exc, bot_token)
        return me, chat_result, chat_error


def diagnose_startup():
    """返回启动界面所需的网络、依赖、Token 与会话检查结果。"""
    settings = db.get_telegram_settings()
    dep_ok, dep_detail = dependency_status()
    network_ok, network_detail = check_network()
    result = {
        'network_ok': network_ok,
        'network_detail': network_detail,
        'dependency_ok': dep_ok,
        'dependency_detail': dep_detail,
        'token_ok': None,
        'token_detail': '未配置',
        'chat_ok': None,
        'chat_detail': '未配置',
        'enabled': bool(settings.get('enabled')),
        'warning_percent': float(settings.get('warning_percent', 80)),
    }
    token = settings.get('bot_token', '').strip()
    chat_id = settings.get('chat_id', '').strip()
    if not token:
        return result
    if not dep_ok:
        result['token_detail'] = '未测试：缺少 python-telegram-bot'
        return result
    if not network_ok:
        result['token_detail'] = '未测试：Telegram 网络不可达'
        return result

    try:
        me, chat, chat_error = asyncio.run(_probe_bot(token, chat_id))
        username = getattr(me, 'username', None)
        result['token_ok'] = True
        result['token_detail'] = f'有效 (@{username})' if username else '有效'
        if chat_id:
            if chat_error:
                result['chat_ok'] = False
                result['chat_detail'] = chat_error
            else:
                title = (
                    getattr(chat, 'title', None)
                    or getattr(chat, 'full_name', None)
                    or str(chat_id)
                )
                result['chat_ok'] = True
                result['chat_detail'] = f'可访问 ({title})'
    except Exception as exc:
        result['token_ok'] = False
        result['token_detail'] = _safe_error(exc, token)
    return result


def notifications_ready():
    settings = db.get_telegram_settings()
    dep_ok, _ = dependency_status()
    return bool(
        dep_ok
        and settings.get('enabled')
        and settings.get('bot_token', '').strip()
        and settings.get('chat_id', '').strip()
    )


async def _send(bot_token, chat_id, text, disable_notification=False):
    Bot = _load_bot_class()
    async with Bot(bot_token) as bot:
        await bot.send_message(
            chat_id=int(chat_id),
            text=text,
            disable_notification=disable_notification,
            connect_timeout=8,
            read_timeout=8,
            write_timeout=8,
            pool_timeout=8,
        )


def send_message(text, disable_notification=False, force=False):
    """同步入口，供 cron 监控和 CLI 测试发送 Telegram 消息。"""
    settings = db.get_telegram_settings()
    token = settings.get('bot_token', '').strip()
    chat_id = settings.get('chat_id', '').strip()
    if not force and not settings.get('enabled'):
        return False, 'Telegram 推送未启用'
    if not token:
        return False, 'Bot Token 未配置'
    if not chat_id:
        return False, '会话 ID 未配置'
    dep_ok, _ = dependency_status()
    if not dep_ok:
        return False, '未安装 python-telegram-bot'
    try:
        asyncio.run(_send(token, chat_id, text, disable_notification))
        return True, '消息已发送'
    except Exception as exc:
        return False, _safe_error(exc, token)


def format_startup_status(result):
    def marker(value):
        if value is True:
            return '正常'
        if value is False:
            return '失败'
        return '未配置'

    return [
        f"Telegram 网络: [{marker(result.get('network_ok'))}] {result.get('network_detail', '')}",
        f"Bot Token: [{marker(result.get('token_ok'))}] {result.get('token_detail', '')}",
        f"推送会话: [{marker(result.get('chat_ok'))}] {result.get('chat_detail', '')}",
    ]


def get_bot_service_status():
    """查询 systemd 长轮询服务状态。"""
    if os.name == 'nt':
        return {'supported': False, 'active': False, 'enabled': False, 'detail': '仅支持 Linux/systemd'}
    try:
        active = subprocess.run(
            ['systemctl', 'is-active', '--quiet', TELEGRAM_SERVICE_NAME],
            capture_output=True, timeout=10,
        ).returncode == 0
        enabled = subprocess.run(
            ['systemctl', 'is-enabled', '--quiet', TELEGRAM_SERVICE_NAME],
            capture_output=True, timeout=10,
        ).returncode == 0
        return {'supported': True, 'active': active, 'enabled': enabled, 'detail': ''}
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        return {'supported': False, 'active': False, 'enabled': False, 'detail': str(exc)}


def _systemd_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _run_systemctl(*args):
    result = subprocess.run(
        ['systemctl', *args], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or f'返回码 {result.returncode}'
    return True, result.stdout.strip()


def install_bot_service():
    """安装并立即启动 Telegram Bot 的 systemd 长轮询服务。"""
    settings = db.get_telegram_settings()
    if not settings.get('bot_token', '').strip():
        return False, '请先配置 Bot Token'
    dep_ok, _ = dependency_status()
    if not dep_ok:
        return False, '请先安装 python-telegram-bot 依赖'
    if not os.path.isfile(TELEGRAM_BOT_SCRIPT):
        return False, f'缺少脚本: {TELEGRAM_BOT_SCRIPT}'
    if os.name == 'nt':
        return False, 'Bot 后台服务仅支持 Linux/systemd'

    unit = f"""[Unit]
Description=PVE Traffic Manager Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={_systemd_quote(BASE_DIR)}
ExecStart={_systemd_quote(PYTHON_PATH)} {_systemd_quote(TELEGRAM_BOT_SCRIPT)}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    directory = os.path.dirname(TELEGRAM_SERVICE_PATH)
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix='.ptm-telegram-', dir=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(unit)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, TELEGRAM_SERVICE_PATH)
        temp_path = None
        for command in (
            ('daemon-reload',),
            ('enable', TELEGRAM_SERVICE_NAME),
            ('restart', TELEGRAM_SERVICE_NAME),
        ):
            ok, detail = _run_systemctl(*command)
            if not ok:
                return False, f"systemctl {' '.join(command)} 失败: {detail}"
        return True, 'Telegram Bot 服务已安装并启动'
    except PermissionError:
        return False, '权限不足，请使用 root 运行 PTM'
    except Exception as exc:
        return False, str(exc)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def uninstall_bot_service():
    """停止并移除 Telegram Bot systemd 服务。"""
    if os.name == 'nt':
        return False, 'Bot 后台服务仅支持 Linux/systemd'
    try:
        _run_systemctl('disable', '--now', TELEGRAM_SERVICE_NAME)
        if os.path.exists(TELEGRAM_SERVICE_PATH):
            os.remove(TELEGRAM_SERVICE_PATH)
        ok, detail = _run_systemctl('daemon-reload')
        if not ok:
            return False, detail
        return True, 'Telegram Bot 服务已卸载'
    except PermissionError:
        return False, '权限不足，请使用 root 运行 PTM'
    except Exception as exc:
        return False, str(exc)


def install_dependency():
    if not os.path.isfile(REQUIREMENTS_PATH):
        return False, f'缺少依赖清单: {REQUIREMENTS_PATH}'
    try:
        result = subprocess.run(
            [
                PYTHON_PATH, '-m', 'pip', 'install', '--upgrade',
                '--target', PYTHON_PACKAGES_DIR, '-r', REQUIREMENTS_PATH,
            ],
            cwd=BASE_DIR, capture_output=True, text=True,
            timeout=300,
        )
        if result.returncode == 0:
            return True, 'Telegram 依赖安装完成'
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, '依赖安装超过 5 分钟，已中止'
    except Exception as exc:
        return False, str(exc)
