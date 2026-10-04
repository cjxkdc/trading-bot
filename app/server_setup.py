"""Configure a server Portal with a hidden password prompt; never a broker password."""
import argparse
from getpass import getpass
import os
from pathlib import Path
import re

from app.control_contracts import ControlError
from portal.access import password_record
from app.origins import validate_server_origin
from app.workbench import atomic_json


def prompt_password():
    # Browser SSH terminals may lack CJK fonts. Keep these messages ASCII.
    print('Type a new Portal password below. Characters and stars will NOT appear.')
    print('Press Enter after each entry. Press Ctrl+C to cancel.')
    while True:
        first = getpass('New Portal password (12-256 characters): ')
        if not 12 <= len(first) <= 256:
            first = None
            print('Password must be 12-256 characters. Please try again.', flush=True)
            continue
        second = getpass('Repeat the SAME password: ')
        if first != second:
            first = second = None
            print('Passwords did not match. Nothing was saved. Please try again.', flush=True)
            continue
        record = password_record(first)
        first = second = None
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--username', default='owner')
    args = parser.parse_args()
    try:
        origin = validate_server_origin(args.origin)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', args.username):
            raise ControlError('Portal username must contain 1-32 letters, digits, underscores or hyphens.')
        record = prompt_password()
        root = Path(__file__).resolve().parents[1]
        folder = root / 'secrets/portal'
        folder.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            folder.chmod(0o700)
        path = folder / 'server.json'
        atomic_json(path, {'origin': origin, 'username': args.username,
                           'password': record, 'session_seconds': 28800})
        if os.name != 'nt':
            path.chmod(0o600)
        print('Server Portal configured. No broker account was connected or changed.')
    except ControlError as exc:
        parser.exit(2, str(exc) + '\n')
    except (KeyboardInterrupt, EOFError):
        parser.exit(2, '\nSetup cancelled. No password was saved.\n')


if __name__ == '__main__':
    main()
