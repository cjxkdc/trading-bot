"""Editable OpenD connection settings; the connector owns environment and market."""
from dataclasses import asdict
import json
import os
from uuid import uuid4

from app.atomic import replace_file
from app.control_contracts import ControlError, project_path
from platforms.opend import OpenDError, PROFILES


def _path(control):
    folder = project_path(control.root, control.profile['config_dir'], under='secrets')
    path = folder / 'connection.json'
    if path.is_symlink():
        raise ControlError('连接配置不能使用符号链接。')
    if path.exists() and path.stat().st_size > 4096:
        raise ControlError('连接配置文件过大，请先检查本地配置。')
    return path


def public_settings(control):
    connector = control.connector()
    try:
        _path(control)
        cfg = connector._settings()
    except (OpenDError, ControlError, OSError):
        return {'values': {'host': '127.0.0.1', 'port': PROFILES[connector.provider]['port']},
                'configured_fields': [], 'issue': '原连接配置无法读取，请检查配置文件后再保存。'}
    return {'values': {'host': cfg.host, 'port': cfg.port},
            'configured_fields': [key for key in ('account_id', 'login_user_id') if getattr(cfg, key)]}


def configure(control, values):
    fields = {'host', 'port', 'account_id', 'login_user_id'}
    if not isinstance(values, dict) or set(values) - fields:
        raise ControlError('连接设置包含此模块不支持的字段。')
    path = _path(control)
    connector = control.connector()
    try:
        cfg = connector._settings()
    except OpenDError:
        raise ControlError('原连接配置无效，请先检查配置文件；不会覆盖账户绑定。') from None
    current = {key: asdict(cfg)[key] for key in fields}
    updated = dict(current)
    for key, value in values.items():
        # Blank protected identity fields mean retain, never unbind/fallback.
        if key in {'account_id', 'login_user_id'} and value == '':
            continue
        updated[key] = value
    if updated['host'] != '127.0.0.1':
        raise ControlError('OpenD 地址请使用 127.0.0.1；云端部署连接云端机器上的 OpenD。')
    if type(updated['port']) is not int or not 1 <= updated['port'] <= 65535:
        raise ControlError('API 端口必须为 1–65535 的整数。')
    for key in ('account_id', 'login_user_id'):
        item = updated[key]
        if item is not None and (not isinstance(item, str) or not item.isascii()
                                 or not item.isdigit() or not 1 <= len(item) <= 32 or int(item) <= 0):
            raise ControlError('账户和登录用户 ID 必须为正整数文本。')
        if key in values and values[key] is None:
            raise ControlError('留空可保留原账户绑定，不能提交空绑定。')
    identity_changed = any(updated[key] != current[key] for key in ('account_id', 'login_user_id'))
    if identity_changed and any((control.state_dir / name).exists() for name in (*connector.state_files, 'journal.sqlite')):
        raise ControlError('此模块已有账户账本，不能改绑其他账户；请使用独立账户配置，原账本会保留。')
    if updated == current:
        return {'message': '连接设置未变化。', 'changed': False}
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == 'posix':
        path.parent.chmod(0o700)
    temporary = path.with_name('.connection-' + uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
            json.dump(updated, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'message': '连接设置已保存，请重新检查账户连接。', 'changed': True}
