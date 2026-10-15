"""Validate and install public source only; local settings and runtime state are preserved.

This updater must come from an already trusted source. A checksum proves integrity,
not authorship: obtain it from the release builder via a trusted channel.
"""
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import uuid
import zipfile

DIRECTORIES = {'app', 'backtests', 'brokers', 'configs', 'data', 'framework', 'strategies',
               'platforms', 'portal', 'tests', 'tools', 'ui', 'deploy', 'docs', 'requirements'}
ROOT_FILES = {'README.md', 'CHANGELOG.md', 'PRODUCT.md', 'bot.py', 'requirements.txt', '.gitignore', 'START-HERE.txt', 'release-manifest.json'}
PRIVATE = {'.git', '.venv', 'secrets', 'runs', '__pycache__', 'node_modules', 'build', '.pytest_cache'}
SUFFIXES = {'.py', '.json', '.md', '.txt', '.js', '.jsx', '.css', '.html', '.svg', '.png', '.sh', '.ps1', '.cmd', '.example'}
LIMIT = 512 * 1024 * 1024


def validate_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or any(ord(c) < 32 for c in name):
        raise ValueError('Invalid release path')
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(p in {'.', '..'} or p in PRIVATE for p in path.parts):
        raise ValueError('Unsafe release path')
    if path.parts[0] not in DIRECTORIES and name not in ROOT_FILES:
        raise ValueError('Unapproved release path')
    if name in {'configs/portal/cloud.json', 'configs/execution_policy.json'}:
        raise ValueError('Private deployment settings cannot be shipped in a release')
    if len(path.parts) > 1 and (path.suffix not in SUFFIXES or path.name == 'connection.json' or any(p.startswith('.') for p in path.parts)):
        raise ValueError('Private or unsupported release file')
    return path


def validate_archive(archive, expected_sha256):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', expected_sha256 or ''):
        raise ValueError('An explicit trusted SHA256 is required')
    archive = Path(archive)
    if archive.stat().st_size > LIMIT:
        raise ValueError('Release archive exceeds size limit')
    raw = archive.read_bytes()
    if sha256(raw).hexdigest() != expected_sha256.lower():
        raise ValueError('Release archive checksum mismatch')
    from io import BytesIO
    contents = {}
    with zipfile.ZipFile(BytesIO(raw)) as bundle:
        total = 0
        for info in bundle.infolist():
            if not info.filename.startswith('Trading-bot/'):
                raise ValueError('Wrong release prefix')
            name = info.filename[len('Trading-bot/'):]
            validate_name(name)
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or info.flag_bits & 1:
                raise ValueError('Only regular, unencrypted release files are supported')
            if name in contents or info.file_size > 100 * 1024 * 1024:
                raise ValueError('Duplicate or oversized release member')
            total += info.file_size
            if total > LIMIT or len(contents) > 20000:
                raise ValueError('Expanded release exceeds limits')
            contents[name] = bundle.read(info)
    try:
        manifest = json.loads(contents['release-manifest.json'])
        records = manifest['sha256']
        if (manifest.get('format_version') != 2 or manifest.get('variant') != 'server'
                or manifest.get('credentials_included') is not False
                or manifest.get('runtime_state_included') is not False
                or manifest.get('execution_policy') not in {'simulation_only', 'account_scoped'} or not isinstance(records, dict)):
            raise ValueError('Unsupported release manifest')
        if set(records) != set(contents) - {'release-manifest.json'}:
            raise ValueError('Manifest does not cover every release file')
        for name, digest in records.items():
            if sha256(contents[name]).hexdigest() != digest:
                raise ValueError('Release member checksum mismatch')
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid release manifest') from exc
    if 'ui/dist/index.html' not in contents:
        raise ValueError('Built Portal is missing')
    return contents


def safe_target(root, name):
    target = root.joinpath(*PurePosixPath(name).parts)
    current = root
    for part in PurePosixPath(name).parts:
        current /= part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('Target path contains a link')
    if not target.resolve().is_relative_to(root):
        raise ValueError('Target escapes project')
    return target


def require_stopped():
    if os.name != 'posix':
        raise ValueError('Applying a server upgrade requires Linux')
    result = subprocess.run(['systemctl', '--user', 'is-active', 'trading-bot.service'],
                            text=True, capture_output=True, check=False)
    # An unavailable user bus is not evidence that the service has stopped.
    if result.returncode != 3 or result.stdout.strip() not in {'inactive', 'failed'}:
        raise ValueError('Stop trading-bot.service gracefully before applying an upgrade')
    group = subprocess.run(['systemctl', '--user', 'show', 'trading-bot.service', '--property=ControlGroup', '--value'],
                           text=True, capture_output=True, check=False)
    if group.returncode != 0:
        raise ValueError('Unable to verify the service control group is empty')
    value = group.stdout.strip()
    if value:
        path = PurePosixPath(value)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Invalid service control group')
        procs = Path('/sys/fs/cgroup') / value.lstrip('/') / 'cgroup.procs'
        if procs.exists() and procs.read_text().strip():
            raise ValueError('A service worker is still running; wait for it to finish')


def atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.upgrade-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def obsolete_files(root, contents):
    manifest = safe_target(root, 'release-manifest.json')
    if not manifest.exists():
        return []
    prior = json.loads(manifest.read_text(encoding='utf-8'))
    if prior.get('format_version') != 2 or not isinstance(prior.get('sha256'), dict):
        raise ValueError('Invalid installed release manifest')
    obsolete = []
    for name, digest in prior['sha256'].items():
        validate_name(name)
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Invalid installed release checksum')
        if name in contents or name.startswith('configs/'):
            continue
        target = safe_target(root, name)
        if target.exists():
            if not target.is_file() or sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Obsolete public file has local edits; review before removal: ' + name)
            obsolete.append(name)
    return sorted(obsolete)


def upgrade(root, archive, expected_sha256, *, apply=False, stopped_check=require_stopped):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError('Use the real existing project directory')
    contents = validate_archive(archive, expected_sha256)
    removed = obsolete_files(root, contents)
    preserved, changed = [], []
    for name, content in contents.items():
        target = safe_target(root, name)
        if name.startswith('configs/'):
            preserved.append(name)
            continue
        if target.exists() and not target.is_file():
            raise ValueError('Release file conflicts with a directory')
        if not target.exists() or target.read_bytes() != content:
            changed.append(name)
    result = {'status': 'validated', 'changed_files': sorted(changed), 'removed_files': removed, 'preserved_config_files': sorted(preserved),
              'private_state': 'untouched', 'backup': None}
    if not apply:
        return result
    stopped_check()
    backup = safe_target(root, 'runs/upgrades/' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    original = {}
    for name in changed + removed:
        target = safe_target(root, name)
        original[name] = {'existed': target.exists(), 'mode': stat.S_IMODE(target.stat().st_mode) if target.exists() else None,
                          'before_sha256': sha256(target.read_bytes()).hexdigest() if target.exists() else None,
                          'installed_sha256': sha256(contents[name]).hexdigest() if name in contents else None}
        if target.exists():
            saved = backup / 'source' / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(target.read_bytes())
    journal = {'archive_sha256': expected_sha256.lower(), 'files': original, 'status': 'prepared'}
    journal_path = backup / 'upgrade.json'
    journal_path.write_text(json.dumps(journal, indent=2), encoding='utf-8')
    # Preserve new config defaults for review, never overwrite active configuration.
    for name in preserved:
        target = backup / 'release-defaults' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[name])
    installed = []
    try:
        for name in changed:
            target = safe_target(root, name)
            installed.append(name)
            atomic_write(target, contents[name], 0o755 if target.suffix == '.sh' else 0o644)
        for name in removed:
            target = safe_target(root, name)
            if sha256(target.read_bytes()).hexdigest() != original[name]['before_sha256']:
                raise ValueError('Obsolete public file changed during upgrade')
            installed.append(name)
            target.unlink()
        journal['status'] = 'installed'
    except BaseException:
        for name in reversed(installed):
            target = safe_target(root, name)
            prior = original[name]
            if prior['existed']:
                atomic_write(target, (backup / 'source' / name).read_bytes(), prior['mode'])
            elif target.exists():
                target.unlink()
        journal['status'] = 'rolled_back'
        raise
    finally:
        journal_path.write_text(json.dumps(journal, indent=2), encoding='utf-8')
    result.update(status='installed', backup=str(backup))
    return result


def rollback(root, backup, *, apply=False, stopped_check=require_stopped):
    root, backup = Path(root).absolute(), Path(backup).absolute()
    if not root.is_dir() or root.resolve() != root or backup.resolve() != backup or not backup.is_relative_to(root / 'runs/upgrades'):
        raise ValueError('Use an existing upgrade backup within this project')
    safe_target(root, backup.relative_to(root).as_posix())
    journal_path = backup / 'upgrade.json'
    if journal_path.is_symlink():
        raise ValueError('Invalid backup journal')
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    if journal.get('status') != 'installed' or not isinstance(journal.get('files'), dict):
        raise ValueError('This backup is not an installed upgrade')
    restore = {}
    for name, record in journal['files'].items():
        validate_name(name)
        if name.startswith('configs/'):
            raise ValueError('Rollback cannot alter active configuration')
        target = safe_target(root, name)
        installed_digest = record['installed_sha256']
        if ((installed_digest is None and target.exists()) or (installed_digest is not None
                and (not target.is_file() or sha256(target.read_bytes()).hexdigest() != installed_digest))):
            raise ValueError('Files changed since this upgrade; review before rollback')
        if record['existed']:
            original = safe_target(root, (backup / 'source' / name).relative_to(root).as_posix())
            content = original.read_bytes()
            if sha256(content).hexdigest() != record['before_sha256']:
                raise ValueError('Backup source checksum mismatch')
            mode = record['mode']
            if type(mode) is not int or mode & ~0o777:
                raise ValueError('Invalid backup mode')
            restore[name] = (content, mode)
        else:
            restore[name] = None
    result = {'status': 'rollback_validated', 'files': sorted(restore), 'private_state': 'untouched'}
    if not apply:
        return result
    stopped_check()
    # Snapshot currently installed public files so a mid-rollback I/O failure
    # can restore the complete current release rather than leaving a mixed tree.
    current = {name: (safe_target(root, name).read_bytes(), stat.S_IMODE(safe_target(root, name).stat().st_mode))
               if safe_target(root, name).exists() else None for name in restore}
    modified = []
    try:
        for name, prior in restore.items():
            target = safe_target(root, name)
            modified.append(name)
            if prior is None:
                target.unlink()
            else:
                atomic_write(target, *prior)
    except BaseException:
        for name in reversed(modified):
            target = safe_target(root, name)
            if current[name] is None:
                if target.exists():
                    target.unlink()
            else:
                atomic_write(target, *current[name])
        raise
    journal['status'] = 'rolled_back'
    journal_path.write_text(json.dumps(journal, indent=2), encoding='utf-8')
    result['status'] = 'rolled_back'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument('--archive', type=Path)
    operation.add_argument('--rollback', type=Path, help='An exact backup directory reported by a prior upgrade')
    parser.add_argument('--sha256')
    parser.add_argument('--apply', action='store_true', help='Default is read-only validation')
    args = parser.parse_args()
    try:
        result = (rollback(args.project, args.rollback, apply=args.apply) if args.rollback else
                  upgrade(args.project, args.archive, args.sha256, apply=args.apply))
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit('Upgrade stopped: ' + str(exc))
