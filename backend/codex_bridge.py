"""File-based IPC between the Docker worker and the host Codex service.

The shared directory carries bounded prompts/results, never Codex credentials.
A renewable lease ensures stopping Docker also stops any host-side generation.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from backend import state
from market_diary.storage import atomic_write_json

PROTOCOL = 1
MAX_BYTES = 2_000_000
LEASE_SECONDS = 15
STEPS = {'analysis', 'review', 'repair', 'connection'}


def directory():
    path = state.RUNTIME / 'codex_bridge'
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def status():
    path = directory() / 'heartbeat.json'
    try:
        info = state.read(path, {})
        online = time.time() - path.stat().st_mtime < LEASE_SECONDS
        return {'online': online, 'ready': online and info.get('ready') is True,
                'version': info.get('version'), 'message': info.get('message') if online else '本机 Codex 执行服务未启动'}
    except (OSError, ValueError):
        return {'online': False, 'ready': False, 'message': '本机 Codex 执行服务未启动'}


def validate_request(value):
    if not isinstance(value, dict) or value.get('protocol') != PROTOCOL:
        raise ValueError('不支持的执行协议')
    if not re.fullmatch(r'[a-f0-9]{24}', str(value.get('task_id', ''))):
        raise ValueError('任务标识无效')
    if value.get('step') not in STEPS:
        raise ValueError('执行步骤无效')
    model = value.get('model', '')
    if not isinstance(model, str) or (model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}', model)):
        raise ValueError('Codex 模型名称无效')
    timeout = value.get('timeout')
    if type(timeout) is not int or not 30 <= timeout <= 900:
        raise ValueError('执行超时无效')
    messages = value.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 12:
        raise ValueError('消息数量无效')
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in {'system', 'user', 'assistant'} or not isinstance(message.get('content'), str):
            raise ValueError('消息格式无效')
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError('分析资料超过本机执行服务上限')
    return value


def call(task_id, step, messages, settings):
    health = status()
    if not health['ready']:
        raise ValueError(health['message'] or '本机 Codex 尚未就绪')
    root = directory()
    request_id = secrets.token_hex(16)
    request = root / f'{request_id}.request.json'
    result = root / f'{request_id}.result.json'
    lease = root / f'{request_id}.lease'
    cancel = root / f'{request_id}.cancel'
    payload = validate_request({'protocol': PROTOCOL, 'task_id': task_id, 'step': step,
                                'model': settings.get('codex_model', ''),
                                'timeout': settings.get('codex_timeout', 600), 'messages': messages})
    lease.touch()
    atomic_write_json(request, payload)
    started = time.monotonic()
    try:
        while time.monotonic() - started < payload['timeout'] + 30:
            task = state.task(task_id)
            if task and (task['cancel_requested'] or task['status'] in {'cancelled', 'interrupted'}):
                raise ValueError('Codex 分析已取消')
            lease.touch()
            if result.exists():
                if result.stat().st_size > MAX_BYTES:raise ValueError('Codex 返回内容过大')
                output = state.read(result)
                if output.get('status') != 'succeeded':raise ValueError(output.get('error', 'Codex 执行失败'))
                if not isinstance(output.get('parsed'), dict):raise ValueError('Codex 返回格式无效')
                return output
            if not status()['online']:raise ValueError('本机 Codex 服务已断开；任务可重试')
            time.sleep(0.5)
        raise ValueError('Codex 分析超时，已请求终止本机进程')
    finally:
        cancel.touch()
        lease.unlink(missing_ok=True)
