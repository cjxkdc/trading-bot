"""Read-only deployment checks; optional private baseline records hashes, not secrets.

No account connects, session tokens, credentials or trade operations are requested.
Use --capture-baseline only before an explicitly authorized deployment; it writes
one owner-private verification file and never changes the project.
"""
import argparse
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlsplit


def file_hashes(root):
    result = {}
    for folder in ('configs', 'secrets'):
        base = root / folder
        if base.is_symlink():
            raise ValueError('Refusing linked configuration directory')
        for path in sorted(base.rglob('*')):
            if path.is_symlink():
                raise ValueError('Refusing linked configuration file')
            if path.is_file():
                result[path.relative_to(root).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return result


def unit_state():
    selected = {'ActiveState', 'SubState', 'MainPID', 'KillMode', 'TimeoutStopUSec', 'Restart', 'UnitFileState'}
    value = subprocess.run(['systemctl', '--user', 'show', 'trading-bot.service', '--property=' + ','.join(sorted(selected))],
                           capture_output=True, text=True, check=False)
    if value.returncode:
        return {'available': False}
    return dict(line.split('=', 1) for line in value.stdout.splitlines() if '=' in line and line.split('=', 1)[0] in selected)


def process_state(root, main_pid, *, proc=None):
    proc = Path('/proc') if proc is None else Path(proc)
    rows = {}
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            details = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            parent = int(details[1])
            # Cmdline is consumed only for classification; never returned or logged.
            argv = (directory / 'cmdline').read_bytes().split(b'\0')
            rows[int(directory.name)] = (parent, argv)
        except (OSError, ValueError, IndexError):
            continue
    descendants = {main_pid} if main_pid > 0 else set()
    while True:
        found = {pid for pid, (parent, _) in rows.items() if parent in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    workers = []
    for pid, (_, argv) in rows.items():
        if not {b'app.portal_worker', b'app.strategy_worker', b'app.strategy_select'}.intersection(argv):
            continue
        try:
            matches_project = (proc / str(pid) / 'cwd').resolve() == root
        except OSError:
            matches_project = False
        if matches_project or pid in descendants:
            workers.append(pid)
    return {'controller_present': main_pid in rows and main_pid > 0,
            'child_process_count': max(0, len(descendants) - 1), 'active_operation_process_count': len(workers)}


def strategy_manager_state(root):
    """Inspect counts without recovery constructors or private instance details."""
    path = root / 'runs/strategy_manager/state.json'
    if not path.exists():
        return dict(state_present=False, corrupt=False, instance_count=0, running_count=0, pending_account_count=0)
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        items, pending = value['instances'], value['pending']
        if value.get('schema') != 1 or not isinstance(items, list) or not isinstance(pending, dict):
            raise ValueError()
        if any(type(item['enabled']) is not bool for item in items):
            raise ValueError()
        return dict(state_present=True, corrupt=False, instance_count=len(items),
                    running_count=sum(item['enabled'] for item in items), pending_account_count=len(pending))
    except (OSError, ValueError, KeyError, TypeError):
        return dict(state_present=True, corrupt=True)


def health(port, host):
    opener = build_opener(ProxyHandler({}))
    result = {}
    for endpoint in ('/health', '/api/auth/status', '/api/state'):
        request = Request(f'http://127.0.0.1:{port}' + endpoint, headers={'Host': host})
        try:
            with opener.open(request, timeout=4) as response:
                code = response.status
                value = json.loads(response.read())
        except HTTPError as error:
            code, value = error.code, {}
        except (OSError, ValueError):
            result[endpoint] = {'reachable': False}
            continue
        entry = {'http_status': code}
        if endpoint == '/health':
            entry.update(application=value.get('application'), execution_policy=value.get('execution_policy'))
        elif endpoint == '/api/auth/status':
            entry.update(enabled=value.get('enabled'), deployment=value.get('deployment'), authenticated=value.get('authenticated'))
        result[endpoint] = entry
    return result


def verify(root, *, baseline=None, capture=False, port=5329, host=None):
    root = Path(root).resolve()
    if host is None:
        settings_path = root / 'secrets/portal/server.json'
        if settings_path.is_file():
            origin = json.loads(settings_path.read_text(encoding='utf-8')).get('origin')
            sys.path.insert(0, str(root))
            from app.origins import validate_origin
            host = urlsplit(validate_origin(origin)).netloc
        else:
            host = '127.0.0.1:' + str(port)
    result = {'project_exists': root.is_dir()}
    if baseline:
        baseline = Path(baseline)
        if baseline.is_symlink():
            raise ValueError('Refusing linked baseline')
        current = file_hashes(root)
        if capture:
            with os.fdopen(os.open(baseline, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as stream:
                json.dump({'project': str(root), 'files': current}, stream)
            result['baseline_captured'] = True
            result['protected_file_count'] = len(current)
        else:
            if os.name == 'posix' and (stat.S_IMODE(baseline.stat().st_mode) & 0o077 or baseline.stat().st_uid != os.getuid()):
                raise ValueError('Baseline must be owner-private')
            before = json.loads(baseline.read_text())
            if before.get('project') != str(root):
                raise ValueError('Baseline belongs to a different project')
            saved = before['files']
            names = set(saved) | set(current)
            result['protected_files_unchanged'] = current == saved
            result['protected_changed_count'] = sum(current.get(name) != saved.get(name) for name in names)
            result['protected_file_count'] = len(current)
    manifest_path = root / 'release-manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        from pathlib import PurePosixPath
        mismatches = 0
        public_count = 0
        for name, expected in manifest['sha256'].items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name:
                raise ValueError('Unsafe manifest path')
            if name.startswith('configs/'):
                continue
            path = root / name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Linked manifest target')
            public_count += 1
            mismatches += not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected
        result['public_release_files'] = public_count
        result['public_release_mismatches'] = mismatches
    else:
        result['public_manifest_present'] = False
    state = root / 'runs/automation/state.json'
    if state.exists():
        automation = json.loads(state.read_text())
        result['automation'] = {'state_present': True, 'enabled': automation.get('enabled') is True, 'status': automation.get('status')}
    else:
        result['automation'] = {'state_present': False, 'enabled': False, 'status': 'default_off'}
    result['strategy_manager'] = strategy_manager_state(root)
    jobs = root / 'runs/portal/jobs'
    unfinished = 0
    for path in jobs.glob('*.json'):
        try:
            unfinished += json.loads(path.read_text()).get('status') == 'running'
        except (OSError, ValueError, AttributeError):
            continue
    result['unfinished_job_records'] = unfinished
    result['service'] = unit_state()
    result['processes'] = process_state(root, int(result['service'].get('MainPID', 0)))
    result['health'] = health(port, host)
    result['dependencies'] = {}
    for package in ('fastapi', 'uvicorn', 'httpx', 'pandas', 'moomoo-api', 'futu-api'):
        try:
            result['dependencies'][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result['dependencies'][package] = None
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--capture-baseline', action='store_true')
    parser.add_argument('--port', type=int, default=5329)
    parser.add_argument('--host', help='Default: configured Portal HTTPS host')
    args = parser.parse_args()
    if args.capture_baseline and not args.baseline:
        parser.error('--capture-baseline requires --baseline')
    try:
        print(json.dumps(verify(args.project, baseline=args.baseline, capture=args.capture_baseline,
                                port=args.port, host=args.host), indent=2))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SystemExit('Deployment verification failed; no private values were printed.')
