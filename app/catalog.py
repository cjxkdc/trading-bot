"""Discover trusted plug-ins without letting an unused broken manifest hide others."""
from hashlib import sha256
import json
from pathlib import Path
import re


class Catalog(dict):
    def __init__(self):
        super().__init__()
        self.issues = []


def scan_catalog(directory, namespace, kinds):
    result, claimed = Catalog(), {}
    namespaces = (namespace,) if isinstance(namespace, str) else namespace
    for path in sorted(Path(directory).glob('*.json')):
        name = None
        try:
            entry = json.loads(path.read_text(encoding='utf-8-sig'))
            name = entry['type']
            if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name):
                raise ValueError('type')
            claimed.setdefault(name, []).append(path)
            module, cls = entry['entrypoint'].split(':')
            if (not any(re.fullmatch(re.escape(n) + r'\.[a-zA-Z_][\w.]*', module) for n in namespaces)
                    or not cls.isidentifier() or entry['kind'] not in kinds
                    or not isinstance(entry.get('version'), str) or not entry['version']):
                raise ValueError('entrypoint')
            if any(not isinstance(entry.get(key, ''), str) for key in ('display_name', 'description')):
                raise ValueError('labels')
            hints = entry.get('parameters', {})
            if not isinstance(hints, dict) or any(not isinstance(v, dict) for v in hints.values()):
                raise ValueError('parameters')
            control = entry.get('control', {})
            if (not isinstance(control, dict)
                    or not isinstance(control.get('capabilities', []), list)
                    or any(not isinstance(c, str) for c in control.get('capabilities', []))
                    or not isinstance(control.get('default_profile', {}), dict)
                    or not isinstance(control.get('settings_fields', []), list)):
                raise ValueError('control')
            fields = control.get('settings_fields', [])
            if any(not isinstance(f, dict) or not isinstance(f.get('name'), str)
                   or f.get('type') not in {'string', 'number', 'integer', 'boolean', 'json'}
                   for f in fields):
                raise ValueError('settings fields')
            auth = control.get('authentication', {})
            if not isinstance(auth, dict) or auth.get('mode', 'local') not in {'local', 'external', 'oauth'}:
                raise ValueError('authentication')
            result[name] = entry
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            result.issues.append(issue(path, '登记文件格式无效，请修复此插件。'))
    # Never choose one of two conflicting registrations, even if one parsed successfully.
    for name, paths in claimed.items():
        if len(paths) > 1:
            result.pop(name, None)
            result.issues = [i for i in result.issues if i['file'] not in {p.name for p in paths}]
            result.issues.extend(issue(p, '模块编号重复；所有同名登记均已停用。') for p in paths)
    return result


def issue(path, message):
    ident = 'invalid_' + sha256(path.name.encode()).hexdigest()[:12]
    return {'type': ident, 'id': ident, 'name': path.stem, 'file': path.name,
            'enabled': False, 'reason': message, 'fields': [], 'defaults': {},
            'capabilities': [], 'description': message, 'kind': 'unavailable', 'version': ''}
