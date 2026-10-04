"""Configure private server-page labels. Never changes Portal authentication."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from app.deployment_metadata import metadata_path, validate_deployment


def configure(root, value, *, replace=False):
    value = validate_deployment(value)
    path = metadata_path(root)
    if path.exists() and not replace:
        raise ValueError('Deployment metadata already exists; use --replace to update it explicitly')
    for folder in (path.parent.parent, path.parent):
        if not folder.exists():
            folder.mkdir(mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.deployment-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        # Recheck before the atomic replacement; never follow a new destination
        # symlink or accidentally change authentication/server.json.
        if metadata_path(root) != path or path.is_symlink():
            raise ValueError('Deployment destination changed')
        if replace:
            os.replace(temporary, path)
        else:
            # Linking the completed temporary file installs without replacing a
            # destination created concurrently by another setup invocation.
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--provider', choices=['aws_lightsail'], required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--instance-name', required=True)
    parser.add_argument('--terminal-url')
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    value = {'provider': args.provider, 'region': args.region, 'instance_name': args.instance_name}
    if args.terminal_url:
        value['terminal_url'] = args.terminal_url
    try:
        configure(args.project, value, replace=args.replace)
    except (OSError, ValueError) as exc:
        parser.exit(2, str(exc) + '\n')
    print('Deployment metadata saved privately. Portal authentication and broker settings were not changed.')


if __name__ == '__main__':
    main()
