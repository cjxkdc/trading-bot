"""Optional systemd supervision for a locally installed, remembered OpenD login.

No passwords are accepted. Finish the official interactive login first. This file
never selects, unlocks or trades an account; broker adapters enforce SIMULATE.
"""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

PORTS = {'moomoo': 11112, 'futu': 11111}


def service_path(value):
    path = Path(value).absolute()
    # Paths are interpolated into systemd, not a shell. Reject every systemd
    # expansion/quoting character rather than attempting ambiguous escaping.
    if path.resolve() != path or any(c.isspace() or c in '%\\"\'$;' or ord(c) < 32 for c in str(path)):
        raise ValueError('Use an absolute service path without spaces or expansion characters')
    return path


def private_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Private OpenD settings must be a regular file')
    if os.name == 'posix' and (path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise ValueError('OpenD private files must belong to this user and have mode 600')


def validate_opend(directory, provider, port=None):
    if provider not in PORTS:
        raise ValueError('Unknown OpenD provider')
    directory = service_path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('Existing official OpenD directory required')
    if os.name == 'posix' and (directory.stat().st_uid != os.getuid() or stat.S_IMODE(directory.stat().st_mode) & 0o077):
        raise ValueError('OpenD directory must belong to this user and have mode 700')
    binary = directory / 'OpenD'
    if binary.is_symlink() or not binary.is_file():
        raise ValueError('Official OpenD binary missing')
    if os.name == 'posix' and (not os.access(binary, os.X_OK) or binary.stat().st_uid != os.getuid()):
        raise ValueError('OpenD binary must be executable and owned by this user')
    configuration = directory / 'OpenD.xml'
    private_file(configuration)
    try:
        raw = configuration.read_bytes()
        if len(raw) > 128 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('Unsupported OpenD XML')
        tree = ET.fromstring(raw)
        values = {}
        for node in tree.iter():
            if node is tree:
                continue
            key = node.tag.lower()
            if key in values:
                raise ValueError('Duplicate OpenD setting')
            values[key] = (node.text or '').strip()
        if values.get('ip', '127.0.0.1') != '127.0.0.1':
            raise ValueError('OpenD must bind loopback')
        if any(values.get(key) for key in ('websocket_port', 'telnet_port')):
            raise ValueError('Disable OpenD WebSocket and Telnet listeners before installing')
        if any(values.get(key) for key in ('login_pwd', 'login_pwd_md5', 'trade_pwd', 'trade_pwd_md5')):
            raise ValueError('Remove password fields; use official remembered login instead')
    except ET.ParseError as exc:
        raise ValueError('Invalid OpenD XML') from exc
    settings_path = directory / 'opend-service.json'
    private_file(settings_path)
    try:
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid private OpenD service settings') from exc
    if not isinstance(settings, dict) or set(settings) - {'provider', 'login_account', 'area_code', 'remembered_login_confirmed'}:
        raise ValueError('Unknown private OpenD service setting')
    if settings.get('provider') != provider or settings.get('remembered_login_confirmed') is not True:
        raise ValueError('Confirm this provider successfully completed remembered login first')
    account = settings.get('login_account')
    if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9@._+\-]{1,128}', account):
        raise ValueError('Unsupported login account format')
    area = settings.get('area_code')
    if area is not None and (not isinstance(area, str) or not re.fullmatch(r'\+[0-9]{1,4}', area)):
        raise ValueError('Invalid country code')
    port = PORTS[provider] if port is None else port
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid OpenD private API port')
    return directory, settings, port


def render(project, directory, provider, port=None):
    project = service_path(project)
    directory, _, port = validate_opend(directory, provider, port)
    if not (project / 'deploy/server/opend_service.py').is_file() or not (project / '.venv/bin/python').is_file():
        raise ValueError('Installed server project and Python environment required')
    return f'''[Unit]
Description=Trading Bot {provider} OpenD gateway
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=3

[Service]
Type=simple
WorkingDirectory={directory}
ExecStart={project}/.venv/bin/python {project}/deploy/server/opend_service.py run --project {project} --opend-dir {directory} --provider {provider} --port {port}
Environment=PYTHONUTF8=1
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
StandardInput=null
StandardOutput=null
StandardError=journal
Restart=on-failure
RestartPreventExitStatus=78
RestartSec=30
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=default.target
'''


def prepare_command(directory, provider, port=None):
    directory, settings, port = validate_opend(directory, provider, port)
    args = [str(directory / 'OpenD'), '-cfg_file=' + str(directory / 'OpenD.xml'),
            '-no_monitor=1', '-api_ip=127.0.0.1', '-api_port=' + str(port),
            '-auto_hold_quote_right=0', '-log_level=no', '-login_by_remember=1',
            '-login_account=' + settings['login_account']]
    if settings.get('area_code'):
        args.append('-area_code=' + settings['area_code'])
    return directory, args, port


def run(directory, provider, port=None):
    directory, args, port = prepare_command(directory, provider, port)
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', port)) == 0:
            raise ValueError('OpenD API port already in use; stop the previous gateway before switching')
    # exec keeps the systemd MainPID equal to OpenD. Console output can include
    # login details, so keep it only inside the owner's private gateway directory.
    os.umask(0o077)
    output = directory / 'service-output.log'
    if output.is_symlink():
        raise ValueError('Refusing linked OpenD console log')
    if output.exists() and output.stat().st_size > 10 * 1024 * 1024:
        rotated = directory / 'service-output.previous.log'
        if rotated.is_symlink():
            raise ValueError('Refusing linked OpenD previous log')
        os.replace(output, rotated)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(output, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    os.chdir(directory)
    os.execv(args[0], args)


def install(project, directory, provider, port=None):
    if os.name != 'posix' or os.geteuid() == 0:
        raise ValueError('Install as the ordinary Ubuntu instance owner')
    unit = render(project, directory, provider, port)
    folder = Path.home() / '.config/systemd/user'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f'trading-bot-opend-{provider}.service'
    if destination.is_symlink():
        raise ValueError('Refusing linked service unit')
    if destination.exists():
        raise ValueError('Service unit already exists; review and replace it explicitly')
    destination.write_text(unit, encoding='utf-8')
    destination.chmod(0o600)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'render', 'install', 'run'])
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--opend-dir', required=True, type=Path)
    parser.add_argument('--provider', required=True, choices=sorted(PORTS))
    parser.add_argument('--port', type=int)
    args = parser.parse_args()
    try:
        if args.action == 'run':
            run(args.opend_dir, args.provider, args.port)
        elif args.action == 'render':
            print(render(args.project, args.opend_dir, args.provider, args.port))
        elif args.action == 'install':
            print('Installed without starting: ' + str(install(args.project, args.opend_dir, args.provider, args.port)))
        else:
            validate_opend(args.opend_dir, args.provider, args.port)
            print('Configuration accepted; remembered login and account access still require a connection test.')
    except (OSError, ValueError, subprocess.SubprocessError):
        print('OpenD supervision is not ready. Review the private configuration and SERVER.md; no password or trade unlock is attempted.', file=sys.stderr)
        raise SystemExit(78)
