"""Trusted callback/public origin validation, shared by hosts and authorization providers."""
import re
from urllib.parse import urlsplit
from app.control_contracts import ControlError


def validate_origin(value):
    parsed = urlsplit(value) if isinstance(value, str) else None
    if (not parsed or parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'}):
        raise ControlError('Portal 地址必须是完整的 HTTPS 地址或本机地址。')
    try:
        parsed.port
    except ValueError:
        raise ControlError('Portal 地址中的端口无效。') from None
    if parsed.scheme != 'https' and parsed.hostname not in {'127.0.0.1', 'localhost'}:
        raise ControlError('远程网页必须使用 HTTPS；HTTP 仅限本机工作台。')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', parsed.hostname):
        raise ControlError('Portal 主机名无效。')
    return value.rstrip('/')


def validate_server_origin(value):
    origin = validate_origin(value)
    if not origin.startswith('https://'):
        raise ControlError('服务器工作台必须使用 HTTPS 地址。')
    return origin
