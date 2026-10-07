"""Read-only support summary. Does not connect to brokers or modify order state.

Run from the project root: python deploy/server/diagnose.py --project .
Only schema-checked states, counts, and hashed references are returned. Credentials,
account numbers, raw exceptions, broker replies and order bodies are never output.
"""
import argparse
from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3


STATES = {'planned', 'unknown', 'pending', 'accepted', 'partial', 'filled',
          'cancelled', 'rejected', 'abandoned', 'running', 'blocked', 'paused',
          'connected', 'ready', 'previewed', 'complete', 'interrupted', 'waiting',
          'disabled', 'failed', 'missed', 'closed'}


def label(value, allowed=STATES):
    return value if isinstance(value, str) and value in allowed else 'unrecognized'


def reference(value):
    return sha256(str(value).encode()).hexdigest()[:12]


def safe_path(root, path):
    path = Path(path)
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise ValueError('Refusing linked runtime state')
    return path


def read_json(root, path):
    path = safe_path(root, path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Runtime record exceeds diagnostic limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid runtime record')
    return value


def timestamp(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9T:.+Z -]{10,40}', value) else None


def order_record(body, state, client, *, adapter=False):
    """Validate JSON objects and compare intent values without coercing types."""
    if not isinstance(client, str) or not client:
        raise ValueError('Invalid order reference')
    raw = json.loads(body)
    saved = json.loads(state) if state is not None else {}
    if (not isinstance(raw, dict) or not isinstance(saved, dict)
            or raw.get('client_id') != client
            or ('client_id' in saved and saved['client_id'] != client)):
        raise ValueError('Invalid order record')
    ticker = raw.pop('broker_symbol', None) if adapter else None
    # JSON encoding preserves bool/number/string distinctions that dict == does not.
    body_key = json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return body_key, saved, ticker


def inspect(root):
    root = Path(root).resolve()
    result = {'read_only': True, 'broker_queries': False}
    automation = root / 'runs/automation/state.json'
    if automation.is_file():
        try:
            value = read_json(root, automation)
            cycles = value.get('cycles', {})
            result['automation'] = {
                'enabled': value.get('enabled') is True,
                'status': label(value.get('status')),
                'last_run_at': timestamp(value.get('last_run', {}).get('at')),
                'cycle_statuses': dict(Counter(label(c.get('status')) for c in cycles.values())),
            }
        except (OSError, ValueError, TypeError, AttributeError):
            result['automation'] = {'readable': False}
    failed = []
    actions = Counter()
    for path in (root / 'runs/portal/jobs').glob('*.json'):
        try:
            value = read_json(root, path)
            action = value.get('action')
            action = action if action in {'observe','status','offline','preview','preflight','execute','prepare',
                'refresh','reconcile','recover','automation','strategy-cycle','strategy-check','strategy-preview',
                'strategy-reconcile','strategy-resolve','resolve-remainder','data-status','configure-data'} else 'other'
            actions[action] += 1
            if value.get('status') in {'blocked','paused','interrupted','failed'}:
                failed.append({'job_ref': reference(value.get('id', path.stem)), 'action': action,
                    'status': label(value.get('status')), 'at': timestamp(value.get('started_at'))})
        except (OSError, ValueError, TypeError, AttributeError):
            actions['unreadable'] += 1
    result['jobs'] = {'action_counts': dict(actions),
                      'recent_failures': sorted(failed, key=lambda j:j['at'] or '', reverse=True)[:10]}
    adapters = {}
    frameworks = []
    unreadable = 0
    unreadable_records = Counter()
    incomplete_directories = set()
    adapter_count = 0
    for path in (root / 'runs').rglob('*.sqlite'):
        # Ignore upgrade copies; inspect only current runtime journals.
        if 'upgrades' in path.relative_to(root).parts:
            continue
        directory = path.parent.resolve()
        try:
            safe_path(root, path)
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if {'binding','orders'} <= tables:
                    for client, body, created, broker, state in db.execute('SELECT client_id,body,created_at,broker_id,state FROM orders'):
                        adapter_count += 1
                        candidate = {'body_key': None, 'entry': None}
                        if isinstance(client, str) and client:
                            # Invalid candidates still count: they cannot make a
                            # second record look like a unique, verified match.
                            adapters.setdefault((directory, client), []).append(candidate)
                        else:
                            incomplete_directories.add(directory)
                        try:
                            body_key, saved, ticker = order_record(body, state, client, adapter=True)
                            candidate.update(body_key=body_key, entry={
                                'broker_id_present': bool(broker), 'adapter_status': label(saved.get('status')),
                                'created_at': timestamp(created),
                                'ticker': ticker if isinstance(ticker,str) and re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}',ticker) else None})
                        except (ValueError, TypeError):
                            unreadable_records['adapter'] += 1
                if {'batches','intents'} <= tables:
                    for client, cycle, body, status, state in db.execute('SELECT id,cycle,body,status,state FROM intents'):
                        body_key = None
                        try:
                            body_key, _, _ = order_record(body, state, client)
                        except (ValueError, TypeError):
                            unreadable_records['framework'] += 1
                        # Keep malformed unresolved rows visible, but never join them.
                        frameworks.append({'client': client, 'directory': directory, 'body_key': body_key,
                                           'batch_ref': reference(cycle), 'status': label(status)})
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            unreadable += 1
            incomplete_directories.add(directory)
    rows = []
    framework_keys = Counter((entry['directory'], entry['client']) for entry in frameworks)
    for entry in frameworks:
        key = (entry['directory'], entry['client'])
        candidates = adapters.get(key, [])
        matches = [c for c in candidates if entry['body_key'] is not None and c['body_key'] == entry['body_key']]
        incomplete = entry['directory'] in incomplete_directories
        match_status = ('unreadable' if incomplete or entry['body_key'] is None else
                        'ambiguous' if len(candidates) > 1 or framework_keys[key] > 1 else
                        'missing' if not candidates else
                        'unreadable' if candidates[0]['body_key'] is None else
                        'matched' if len(matches) == 1 else 'mismatched')
        account_ref = reference(entry['directory'].relative_to(root).as_posix())
        row = {'order_ref': reference(account_ref + ':' + str(entry['client'])), 'account_ref': account_ref,
               'batch_ref': entry['batch_ref'], 'framework_status': entry['status'],
               'framework_record_readable': entry['body_key'] is not None,
               'adapter_candidate_count': len(candidates), 'adapter_match_count': len(matches),
               'adapter_match_status': match_status, 'adapter_scan_incomplete': incomplete}
        if match_status == 'matched':
            row.update(matches[0]['entry'])
        rows.append(row)
    def priority(row):
        if row['framework_status'] in {'unknown', 'unrecognized'} and row['adapter_match_status'] != 'matched':
            return 0
        return 1 if row['framework_status'] not in {'filled', 'abandoned'} else 2
    ordered = sorted(sorted(rows, key=lambda r:r.get('created_at') or '', reverse=True), key=priority)
    listed = ordered[:40]
    result['orders'] = {'framework_count': len(frameworks), 'adapter_count': adapter_count,
        'status_counts': dict(Counter(x['status'] for x in frameworks)), 'unreadable_databases': unreadable,
        'unreadable_records': sum(unreadable_records.values()), 'unreadable_record_counts': dict(unreadable_records),
        'recent_or_unresolved': listed, 'truncated_count': max(0, len(rows)-len(listed)),
        'unresolved_omitted': sum(r['framework_status'] not in {'filled', 'abandoned'} for r in ordered[40:]),
        'unknown_without_adapter': sum(r['framework_status']=='unknown' and r['adapter_match_status']=='missing' for r in rows),
        'unknown_unverified_adapter': sum(r['framework_status']=='unknown' and r['adapter_match_status'] not in {'missing','matched'} for r in rows)}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.project), ensure_ascii=False, indent=2))
