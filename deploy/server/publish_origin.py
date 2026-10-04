"""Change only the existing Portal URL, preserving its password and all account state."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.origins import validate_server_origin
from app.workbench import atomic_json
from deploy.server.upgrade import require_stopped
from portal.access import RemoteSettings


def migrate(root, origin, *, apply=False, stopped_check=require_stopped):
    root = Path(root).resolve(strict=True)
    path = root / 'secrets/portal/server.json'
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('Use the real private Portal configuration')
    raw = json.loads(path.read_text(encoding='utf-8'))
    origin = validate_server_origin(origin)
    updated = {**raw, 'origin': origin}
    RemoteSettings.from_record(updated)
    if apply and raw != updated:
        stopped_check()
        # Backup remains private and can restore the old URL without resetting a password.
        backup = root / 'runs/upgrades/public-web-origin.json'
        if backup.exists():
            raise ValueError('Origin migration backup already exists; review it before migrating again')
        atomic_json(backup, raw)
        if os.name != 'nt':
            backup.chmod(0o600)
        atomic_json(path, updated)
        if os.name != 'nt':
            path.chmod(0o600)
    return {'status': 'installed' if apply else 'validated', 'origin': origin,
            'login_preserved': raw['password'] == updated['password'],
            'account_state': 'untouched'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.project, args.origin, apply=args.apply), indent=2))
