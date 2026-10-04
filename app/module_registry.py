"""Control-plane composition. Provider names and UI forms come from manifests."""
from copy import deepcopy
from hashlib import sha256
import importlib
import inspect
import json
from pathlib import Path
import re

from app.assembly import strategy_catalog, data_catalog, broker_catalog, build_portfolio_runner, strategy_data_requirements
from app.control_contracts import ControlError, project_path
from framework.configuration import load_config, normalize_config
from framework.decisions import digest
from app.strategy_documentation import strategy_documentation

ALLOWED_MODES = {'paper', 'broker_paper'}
MODE_LABELS = {'paper': '本地回放', 'broker_paper': '模拟账户', 'live': '实盘'}


def load_class(entrypoint, namespace):
    if not isinstance(entrypoint, str) or not re.fullmatch(
            re.escape(namespace) + r'\.[A-Za-z_][\w.]*:[A-Za-z_]\w*', entrypoint):
        raise ControlError('模块入口必须属于对应的本地模块目录。')
    module, name = entrypoint.split(':')
    try:
        return getattr(importlib.import_module(module), name)
    except Exception:
        # Third-party import failures can contain secrets or environment details.
        raise ControlError('模块无法加载，请检查其依赖和入口；其他模块仍可使用。') from None


class ModuleRegistry:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def catalog(self, kind):
        function = {'strategy': strategy_catalog, 'data': data_catalog, 'broker': broker_catalog}[kind]
        folder = {'strategy': 'strategies', 'data': 'data', 'broker': 'brokers'}[kind]
        return function(self.root / folder / 'plugins')

    def profile_file(self, kind):
        name = {'data': 'data_sources.json', 'broker': 'broker_profiles.json'}[kind]
        value = json.loads((self.root / 'configs' / name).read_text(encoding='utf-8-sig'))
        key = 'sources' if kind == 'data' else 'profiles'
        if not isinstance(value.get(key), dict) or not isinstance(value.get('default'), str):
            raise ControlError('模块实例配置缺少 default 或实例列表。')
        return value, key

    def profiles(self, kind):
        value, key = self.profile_file(kind)
        result = deepcopy(value[key])
        catalog = self.catalog(kind)
        if kind == 'broker':
            for ident, profile in list(result.items()):
                if isinstance(profile, dict) and set(profile) == {'config_file'}:
                    try:
                        result[ident] = deepcopy(load_config(project_path(self.root, profile['config_file']))['broker'])
                    except (OSError, ValueError, KeyError, TypeError):
                        result[ident] = {'_profile_error': '此账户实例配置无法读取。'}
        # A newly registered module with a default instance appears without any Portal edit.
        configured = {p.get('type') for p in result.values() if isinstance(p, dict)}
        for name, entry in catalog.items():
            defaults = entry.get('control', {}).get('default_profile')
            if name not in configured and defaults is not None and name not in result:
                result[name] = dict(defaults, type=name)
        for ident, profile in result.items():
            if not re.fullmatch(r'[a-z][a-z0-9_-]*', ident) or not isinstance(profile, dict):
                result[ident] = {'_profile_error': '模块实例编号或配置无效。'}
        return result

    def default(self, kind):
        return self.profile_file(kind)[0]['default']

    def resolve(self, kind, ident):
        try:
            profile = deepcopy(self.profiles(kind)[ident])
            if profile.get('_profile_error'):
                raise ControlError(profile['_profile_error'])
            entry = self.catalog(kind)[profile['type']]
        except (KeyError, TypeError):
            raise ControlError('所选模块已不存在，请检查模块注册与实例配置。') from None
        if not entry.get('control', {}).get('entrypoint'):
            raise ControlError('这个模块尚未声明工作台控制接口。')
        return profile, deepcopy(entry)

    def descriptor(self, kind, ident):
        profile, entry = self.resolve(kind, ident)
        control = entry['control']
        mode = control.get('mode')
        enabled = kind != 'broker' or mode in ALLOWED_MODES
        if enabled:
            cls = load_class(control['entrypoint'], {'data': 'data', 'broker': 'brokers'}[kind])
            methods = ({'collect': ('collect',), 'inspect': ('inspect',), 'configure': ('configure',),
                        'cancel': ('cancel',), 'preview': ('load',), 'upload': ('load',), 'paper_signal': ('paper_signal',)}
                       if kind == 'data' else {'inspect': ('inspect',), 'execute': ('session',),
                                             'prepare': ('observe_account', 'observe_quotes'),
                                             'calendar': ('trading_sessions',), 'reconcile': ('reconciliation_session',)})
            for capability in control.get('capabilities', []):
                if any(not callable(getattr(cls, method, None)) for method in methods.get(capability, ())):
                    raise ControlError('模块声明了尚未实现的操作，请修复其控制接口。')
        return {'id': ident, 'type': entry['type'], 'name': entry.get('display_name', entry['type']),
                'description': entry.get('description', ''), 'version': entry['version'],
                'capabilities': control.get('capabilities', []),
                'fields': control.get('settings_fields', []), 'mode': mode,
                'mode_label': MODE_LABELS.get(mode, ''), 'enabled': enabled,
                'authentication': control.get('authentication', {'mode': 'local' if mode == 'paper' else 'external',
                    'title': '本地回放' if mode == 'paper' else '券商客户端或 API 配置'}),
                'reason': None if enabled else '当前工作台仅启用模拟与本地回放。'}

    def descriptors(self, kind):
        result = []
        for ident in self.profiles(kind):
            try:
                result.append(self.descriptor(kind, ident))
            except ControlError as exc:
                result.append(self.unavailable(ident, str(exc)))
        result.extend(dict(i) for i in self.catalog(kind).issues)
        return result

    @staticmethod
    def unavailable(ident, reason):
        return {'id': ident, 'type': ident, 'name': ident, 'enabled': False, 'reason': reason,
                'description': reason, 'capabilities': [], 'fields': [], 'mode': None,
                'mode_label': '不可用', 'authentication': {'mode': 'unavailable'}}

    def control(self, kind, ident, *, config=None, bind_request=True):
        profile, entry = self.resolve(kind, ident)
        if kind == 'broker' and entry['control'].get('mode') not in ALLOWED_MODES:
            raise ControlError('当前执行权限不允许使用此账户环境。')
        cls = load_class(entry['control']['entrypoint'], {'data': 'data', 'broker': 'brokers'}[kind])
        adapter = cls(self.root, profile, entry)
        adapter.instance_id = ident
        if kind == 'data' and bind_request and entry['control'].get('request_driven'):
            cfg = config or normalize_config(load_config(self.root / 'configs/portfolio_config.json'))
            adapter = adapter.for_request(self.data_requirements(cfg))
        return adapter

    def data_requirements(self, cfg):
        try:
            return strategy_data_requirements(cfg['strategy'])
        except (ValueError, TypeError, KeyError, ImportError, AttributeError):
            raise ControlError('策略尚未正确声明股票范围和所需数据字段。') from None

    def canonical_id(self, kind, ident):
        if ident in self.profiles(kind):
            return ident
        matches = []
        for name in self.profiles(kind):
            try:
                if ident in self.resolve(kind, name)[1].get('control', {}).get('profile_aliases', []):
                    matches.append(name)
            except ControlError:
                continue
        return matches[0] if len(matches) == 1 else ident

    def configuration(self, broker_id):
        cfg = normalize_config(load_config(self.root / 'configs/portfolio_config.json'))
        profile, entry = self.resolve('broker', broker_id)
        cfg['broker'] = deepcopy(profile)
        # Connection construction and canonical ledger ownership belong to the adapter.
        if entry['control'].get('mode') not in ALLOWED_MODES:
            raise ControlError('当前执行权限不允许实盘。')
        return cfg

    def version(self):
        return sha256((self.root / 'configs/portfolio_config.json').read_bytes()).hexdigest()

    def connection_context(self, broker_id):
        profile, entry = self.resolve('broker', broker_id)
        return digest({'broker_id': broker_id, 'profile': profile, 'entry': entry,
                       'revision': self.control('broker', broker_id).config_revision()})

    def source_context(self, source_id):
        profile, entry = self.resolve('data', source_id)
        return digest({'source_id': source_id, 'profile': profile, 'entry': entry,
                       'revision': self.control('data', source_id, bind_request=False).config_revision()})

    def context(self, broker_id, source_id):
        cfg = self.configuration(broker_id)
        data_profile, data_entry = self.resolve('data', source_id)
        broker_profile, broker_entry = self.resolve('broker', broker_id)
        strategy = self.catalog('strategy')[cfg['strategy']['type']]
        return digest({'configuration': cfg, 'source_id': source_id, 'broker_id': broker_id,
                       'data_requirements': self.data_requirements(cfg).record() if data_entry['control'].get('request_driven') else None,
                       'source_profile': data_profile, 'source_entry': data_entry,
                       'broker_profile': broker_profile, 'broker_entry': broker_entry,
                       'strategy_entry': strategy,
                       'source_revision': self.control('data', source_id, config=cfg).config_revision(),
                       'connection_revision': self.control('broker', broker_id).config_revision()})

    def compatible(self, cfg, source_id):
        _, entry = self.resolve('data', source_id)
        strategy = self.catalog('strategy')[cfg['strategy']['type']]
        schemas = entry.get('strategy_schemas', [entry.get('strategy_schema')])
        if strategy['kind'] != 'portfolio' or strategy.get('input_schema') not in schemas:
            raise ControlError('此数据模块与当前策略的输入格式不兼容。')
        if entry['control'].get('request_driven'):
            try:
                self.control('data', source_id, config=cfg).validate_request()
            except ValueError as exc:
                raise ControlError(str(exc)) from None

    def strategies(self):
        result = []
        catalog = self.catalog('strategy')
        for name, entry in catalog.items():
            documentation = strategy_documentation(self.root / 'strategies', entry.get('readme'))
            try:
                cls = load_class(entry['entrypoint'], 'strategies')
                parameters = inspect.signature(cls).parameters
            except (ControlError, ValueError, TypeError):
                result.append({'type': name, 'name': entry.get('display_name', name),
                               'version': entry['version'], 'enabled': False, 'fields': [], 'defaults': {},
                               'description': entry.get('description', ''),
                               **documentation,
                               'reason': '策略无法加载，请检查依赖与入口。'})
                continue
            fields, defaults = [], {}
            hints = entry.get('parameters', {})
            for key, param in parameters.items():
                if param.kind in (param.VAR_KEYWORD, param.VAR_POSITIONAL):
                    continue
                value = param.default if param.default is not inspect.Parameter.empty else None
                kind = ('boolean' if isinstance(value, bool) else 'integer' if isinstance(value, int)
                        else 'number' if isinstance(value, float) else 'json' if isinstance(value, (dict, list, tuple))
                        else 'string')
                fields.append(dict({'name': key, 'label': key, 'type': kind, 'required': value is None}, **hints.get(key, {})))
                defaults[key] = value
            enabled = entry['kind'] == 'portfolio'
            result.append({'type': name, 'name': entry.get('display_name', name),
                           'description': entry.get('description', ''), 'version': entry['version'],
                           'defaults': defaults, 'fields': fields, 'enabled': enabled,
                           **documentation,
                           'reason': None if enabled else '当前固定框架使用组合接口，此模块使用单标的接口。'})
        return result + catalog.issues

    def validate_strategy(self, cfg):
        strategy = cfg['strategy']
        entry = self.catalog('strategy').get(strategy.get('type'), {})
        if entry.get('kind') != 'portfolio':
            raise ControlError('请选择与当前框架兼容的组合策略。')
        try:
            build_portfolio_runner(normalize_config(cfg))
            for rule in entry.get('limit_rules', []):
                value = 1
                for field in rule['product']:
                    value *= strategy[field]
                if value > float(cfg['limits'][rule['limit']]) + 1e-12:
                    raise ValueError('limit')
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ControlError('策略参数无效或超过已有仓位上限。') from None

    def inventory(self):
        return {kind: [{'type': e['type'], 'name': e.get('display_name', e['type']), 'kind': e['kind'],
                        'has_control': bool(e.get('control')), 'description': e.get('description', '')}
                       for e in self.catalog(kind).values()] for kind in ('broker', 'data')}
