"""Optional private deployment labels and strict links to the official AWS console."""
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

CONSOLE_URL = 'https://lightsail.aws.amazon.com/'
FIELDS = {'provider', 'region', 'instance_name', 'terminal_url'}
EMPTY = {'provider': None, 'region': None, 'instance_name': None}
REGION = re.compile(r'(?:af|ap|ca|eu|il|me|mx|sa|us)-(?:gov-)?[a-z]+-[1-9][0-9]?')
INSTANCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}')


def validate_deployment(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('Unknown deployment metadata field')
    if raw.get('provider') != 'aws_lightsail':
        raise ValueError('Unsupported deployment provider')
    region, instance = raw.get('region'), raw.get('instance_name')
    if not isinstance(region, str) or not REGION.fullmatch(region):
        raise ValueError('Invalid AWS region')
    if not isinstance(instance, str) or not INSTANCE.fullmatch(instance):
        raise ValueError('Invalid Lightsail instance name')
    result = {'provider': 'aws_lightsail', 'region': region, 'instance_name': instance}
    terminal = raw.get('terminal_url')
    if terminal is None:
        return result
    if not isinstance(terminal, str) or len(terminal) > 2048 or any(c.isspace() or ord(c) < 32 or c == '\\' for c in terminal):
        raise ValueError('Invalid official terminal URL')
    try:
        parsed = urlsplit(terminal)
        # Only AWS's observed remote SSH terminal route, scoped to this exact
        # configured region and instance. No commands, redirect parameters or
        # signed session credentials may be persisted in deployment metadata.
        host = parsed.hostname or ''
        official_host = re.fullmatch(r'(?:[a-z0-9-]+\.)?' + re.escape(region) + r'\.console\.aws\.amazon\.com', host)
        expected = f'/lightsail/remote/{region}/instances/{instance}/terminal'
        if (parsed.scheme != 'https' or not official_host or parsed.port is not None
                or parsed.username is not None or parsed.password is not None
                or parsed.path != expected or parsed.query != 'protocol=ssh' or parsed.fragment):
            raise ValueError('Use the official AWS browser SSH terminal URL for this instance')
    except (ValueError, UnicodeError):
        raise ValueError('Use the official AWS browser SSH terminal URL for this instance') from None
    result['terminal_url'] = terminal
    return result


def metadata_path(root):
    root = Path(root).resolve()
    path = root / 'secrets/portal/deployment.json'
    current = root
    for part in ('secrets', 'portal', 'deployment.json'):
        current /= part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('Deployment metadata cannot use linked paths')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Deployment metadata must stay inside the project')
    return path


def load_deployment(root):
    try:
        path = metadata_path(root)
        if not path.is_file() or path.stat().st_size > 4096:
            return dict(EMPTY)
        if os.name == 'posix' and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077):
            return dict(EMPTY)
        return validate_deployment(json.loads(path.read_text(encoding='utf-8-sig')))
    except (OSError, ValueError, TypeError):
        return dict(EMPTY)


def deployment_links(metadata):
    try:
        value = validate_deployment(metadata)
    except (ValueError, TypeError):
        return {'console': None, 'terminal': None}
    return {'console': CONSOLE_URL, 'terminal': value.get('terminal_url')}
