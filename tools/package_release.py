"""One source tree, two public releases. Private state is excluded by allowlist.

python tools/package_release.py --output runs/releases
The desktop EXE is optional; source launchers work without a bundled interpreter.
"""
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {'app', 'backtests', 'brokers', 'configs', 'data', 'framework', 'strategies',
               'platforms', 'portal', 'tests', 'tools', 'ui', 'deploy', 'docs', 'requirements'}
ROOT_FILES = {'README.md', 'bot.py', 'requirements.txt', '.gitignore'}
SKIP = {'.git', '.venv', 'secrets', 'runs', '__pycache__', 'node_modules', 'build', '.pytest_cache'}
SUFFIXES = {'.py', '.json', '.md', '.txt', '.js', '.jsx', '.css', '.html', '.svg', '.png',
            '.sh', '.ps1', '.cmd', '.example'}


def public_files(root, variant):
    if variant not in {'desktop', 'server'}:
        raise ValueError('Unknown deployment variant')
    root = Path(root).resolve()
    files = []
    for name in sorted(ROOT_FILES):
        if (root / name).is_file() and not (root / name).is_symlink():
            files.append(root / name)
    for folder in sorted(DIRECTORIES):
        base = root / folder
        if not base.is_dir() or base.is_symlink() or getattr(base, 'is_junction', lambda: False)():
            continue
        for current, directories, names in os.walk(base, followlinks=False):
            current = Path(current)
            directories[:] = [name for name in directories if name not in SKIP
                and not (current / name).is_symlink()
                and not getattr(current / name, 'is_junction', lambda: False)()]
            for name in sorted(names):
                path = current / name
                relative = path.relative_to(root)
                if relative.parts[:3] == ('tests', 'fixtures', 'strategy_samples'):
                    continue  # Test-only plugins never belong to an installed strategy library.
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                if (path.suffix not in SUFFIXES or name == 'connection.json' or name.startswith('.')
                        or relative.as_posix() in {'configs/portal/cloud.json', 'configs/execution_policy.json'}):
                    continue
                files.append(path)
    return sorted(set(files))


def package(root, output, variant, *, launcher=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    files = public_files(root, variant)
    if not (root / 'ui/dist/index.html').is_file():
        raise ValueError('Build the Portal frontend before packaging')
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'Trading-bot-{variant}.zip'
    records = {}
    temporary = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            name = path.relative_to(root).as_posix()
            content = path.read_bytes()
            if path.suffix == '.ps1':
                content = content.decode('utf-8-sig').encode('utf-8-sig')  # Windows PowerShell 5.1
            info = zipfile.ZipInfo('Trading-bot/' + name)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if path.suffix == '.sh' else 0o100644) << 16
            bundle.writestr(info, content)
            records[name] = sha256(content).hexdigest()
        if variant == 'desktop' and launcher:
            content = Path(launcher).read_bytes()
            if content[:2] != b'MZ':
                raise ValueError('Invalid Windows launcher')
            bundle.writestr('Trading-bot/Trading Bot.exe', content)
            records['Trading Bot.exe'] = sha256(content).hexdigest()
        instruction = ('服务器包：先读 docs/SERVER.md，在 Ubuntu 实例运行 deploy/server/install.sh。'
                       if variant == 'server' else
                       '桌面包：先运行 deploy/desktop/Install Local.cmd，再使用启动器；账户配置不随包传递。')
        content = (instruction + '\n两种部署共用源码；服务器包不包含 Windows exe。\n').encode('utf-8')
        bundle.writestr('Trading-bot/START-HERE.txt', content)
        records['START-HERE.txt'] = sha256(content).hexdigest()
        bundle.writestr('Trading-bot/release-manifest.json', json.dumps({
            'format_version': 2, 'variant': variant, 'created_at': datetime.now(timezone.utc).isoformat(),
            'credentials_included': False, 'runtime_state_included': False,
            'execution_policy': 'account_scoped', 'sha256': records}, indent=2))
    os.replace(temporary, archive)
    archive.with_suffix('.zip.sha256').write_text(sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n', encoding='ascii')
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--launcher', type=Path)
    args = parser.parse_args()
    for variant in ('desktop', 'server'):
        path = package(ROOT, args.output, variant, launcher=args.launcher)
        print(f'{path.name}: {path.stat().st_size} bytes')
