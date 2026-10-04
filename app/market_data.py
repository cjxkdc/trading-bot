"""Data operations through the same workbench service used by the Portal."""
import argparse
from getpass import getpass
import json
from app.control_contracts import ControlError
from app.workbench_cli import WorkbenchClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('configure', 'status', 'collect'))
    parser.add_argument('--source', help='Optional registered data instance; default is the workbench selection')
    parser.add_argument('--url')
    parser.add_argument('--requests-per-minute', type=int)
    parser.add_argument('--connect', action='store_true')
    args = parser.parse_args()
    try:
        client = WorkbenchClient(url=args.url).connect()
        if args.source:
            client.request('/api/source', {'source': args.source})
        state = client.request('/api/state')
        source = next(s for s in state['sources'] if s['id'] == state['source'])
        if args.action == 'configure':
            if not state['actions']['configure-data']['enabled']:
                raise ControlError(state['actions']['configure-data']['message'])
            values = {}
            for field in source['fields']:
                name = field['name']
                if name == 'requests_per_minute' and args.requests_per_minute is not None:
                    values[name] = args.requests_per_minute
                    continue
                raw = (getpass if field.get('secret') else input)(field.get('label', name) + ' (留空保留): ')
                if not raw:
                    continue
                kind = field['type']
                values[name] = int(raw) if kind == 'integer' else float(raw) if kind == 'number' else json.loads(raw) if kind in {'json', 'boolean'} else raw
            result = client.request('/api/data/settings', values)
        elif args.action == 'status' and not args.connect:
            result = source
        else:
            result = client.operate('data-status' if args.action == 'status' else 'refresh')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ControlError, ValueError) as exc:
        print(str(exc) if isinstance(exc, ControlError) else '配置值格式无效。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
