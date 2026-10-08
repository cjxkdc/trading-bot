"""Portal state and jobs. No trading logic or vendor SDK lives in the UI layer."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from backtests.experiments import (compare_results, import_result, read_json,
                                  validate_export, verified_record)
from backtests.quantconnect.results import parse_result
from bot import export_backtest
from framework.configuration import load_config, normalize_config
from app.control_contracts import ControlError
from app.diagnostics import failure_details
from app.operations import WORKBENCH_ACTIONS, BROKER_CHECKS, DATA_CAPABILITIES, context_for
from app.readiness import operation_states
from app.module_registry import ModuleRegistry
from app.market_status import selected_market_session
from app.task_client import run_worker
from app.broker_session import BrokerSession
from app.broker_authorization import BrokerAuthorization
from framework.journal_reader import JournalReader, JournalReadError
from app.automation import Automation
from app.monitoring import Monitoring
from app.strategy_instances import StrategyInstances
from app.strategy_files import StrategyFiles
from app.execution_policy import CURRENT_EXECUTION_POLICY, load_execution_policy

MAX_FILE = 32 * 1024 * 1024
PortalError = ControlError
_UNSPECIFIED_STRATEGY = object()


def check_expected_strategy(expected, actual):
    """Bind new workspace exports to the strategy the user is viewing.

    Omission keeps older clients compatible; explicit null or other malformed
    values are rejected. Call under the service lock, before writing files.
    """
    if expected is _UNSPECIFIED_STRATEGY:
        return
    if not isinstance(expected, str) or not expected.strip():
        raise PortalError('预期策略编号必须为非空文本。')
    if expected != actual:
        raise PortalError('配置的策略已变化，请刷新策略工作区后重新导出。')


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def public(value, secret_fields=(), secret_values=()):
    # Worker capabilities and credentials are never sent to the browser or disk log.
    hidden = {'api_key', 'preview_token', 'account_id', 'login_user_id',
              'access_token', 'refresh_token', 'code_verifier', 'authorization_code',
              'account_fingerprint', 'account_ref', 'telegram_token', 'telegram_chat_id', 'heartbeat_url', 'snapshot_path', 'config_dir', *secret_fields}
    if isinstance(value, dict):
        return {k: public(v, secret_fields, secret_values) for k, v in value.items() if k not in hidden}
    if isinstance(value, (tuple, list)):
        return [public(v, secret_fields, secret_values) for v in value]
    if isinstance(value, str):
        for secret in secret_values:
            if secret:
                value = value.replace(secret, '[redacted]')
    return value


class PortalService:
    def __init__(self, root, worker=run_worker):
        self.root = Path(root).resolve()
        self.folder = self.root / 'runs/portal'
        self.worker = worker
        self.registry = ModuleRegistry(self.root)
        self.lock = threading.RLock()
        self.active = None
        self.thread = None
        self.closing = False
        self.preview = None
        self.preview_private = None
        self.preview_context = None
        self.account = None
        self.broker_session = BrokerSession()
        self.authorizations = BrokerAuthorization(self.registry)
        self.authorization_notice = None
        self.jobs = []
        self.snapshots = {}
        self.snapshot_cache = {}
        self.selected_snapshot = None
        self.source = self.registry.default('data')
        self.broker_id = self.registry.default('broker')
        self.started_at = now()
        self.folder.mkdir(parents=True, exist_ok=True)
        prefs = self.folder / 'preferences.json'
        if prefs.is_file():
            try:
                saved = read_json(prefs)
                self.source = self.registry.canonical_id('data', saved.get('source', self.source))
                self.broker_id = saved.get('broker', self.broker_id)
            except (OSError, ValueError, AttributeError):
                pass
        for path in sorted((self.folder / 'jobs').glob('*.json'), reverse=True)[:40]:
            try:
                item = read_json(path)
                if item.get('status') == 'running':
                    item.update(status='interrupted', message='上次服务退出前未取得最终结果；请先核对记录。')
                    if item.get('action') == 'preflight':
                        for step in item.get('steps', []):
                            if step.get('status') == 'running':
                                step.update(status='blocked', message='上次检查被服务退出中断，请重新检查。')
                            elif step.get('status') == 'pending':
                                step.update(status='skipped', message='前置检查中断，尚未执行。')
                        item['cancel_available'] = False
                self.jobs.append(item)
            except (OSError, ValueError, AttributeError):
                continue
        self.jobs.sort(key=lambda j: j.get('started_at', ''), reverse=True)
        self.automation = Automation(self.root, self.registry)
        self.monitor = Monitoring(self.root, self.registry)
        self.strategy_instances = StrategyInstances(self.root, self.registry, lock=self.lock, recover_pending=True)
        self.strategy_files = StrategyFiles(self.root, self.registry)
        self.scheduler_stop = threading.Event()
        self.scheduler_wake = threading.Event()
        self.scheduler_thread = None

    def idle(self):
        if self.closing:
            raise PortalError('本地服务正在关闭。')
        if self.active:
            raise PortalError('上一项操作仍在运行，请等待完成。')

    def clear_preview(self):
        self.preview = self.preview_private = None
        self.preview_context = None

    def config(self):
        return normalize_config(load_config(self.root / 'configs/portfolio_config.json')), self.registry.version()

    def _public(self, value, secret_values=()):
        fields = {f['name'] for kind in ('data', 'broker') for entry in self.registry.descriptors(kind)
                  for f in entry.get('fields', []) if f.get('secret')}
        return public(value, fields, secret_values)

    def strategies(self):
        return self.registry.strategies()

    def strategy_instance_action(self, action, value, ident=None):
        with self.lock:
            manager = self.strategy_instances
            if action == 'stop':
                if value:
                    raise PortalError('停止实例不接受交易参数。')
                return manager.stop(ident)
            self.idle()
            if action == 'create':
                return manager.create(value)
            if action == 'update':
                return manager.update(ident, value)
            if action == 'limits':
                return manager.configure_limits(value)
            if action == 'remove':
                if value:
                    raise PortalError('删除实例不接受交易参数。')
                return manager.remove(ident)
            if action in {'start', 'once'}:
                item = manager.get(ident)
                if value != {'confirmation': ident}:
                    raise PortalError('请确认所选策略实例和账户。')
                if self.automation.data['enabled'] and self.same_account_profile(self.automation.data['binding']['broker_id'], item['broker_id']):
                    raise PortalError('此账户的旧自动计划仍在运行，请先关闭，再启动策略实例。')
                result = manager.start(ident) if action == 'start' else manager.run_once(ident)
                self.scheduler_wake.set()
                return result
            raise PortalError('未知策略实例操作。')

    def same_account_profile(self, first, second):
        if first == second:
            return True
        a = getattr(self.registry.control('broker', first), 'state_dir', None)
        b = getattr(self.registry.control('broker', second), 'state_dir', None)
        return a is not None and a == b

    def managed_account(self, broker_id):
        if self.strategy_instances.corrupt:
            return True
        if self.strategy_instances.owns_account(broker_id):
            return True
        try:
            return any(self.strategy_instances.owns_account(x['broker_id'])
                and self.same_account_profile(broker_id, x['broker_id'])
                for x in self.strategy_instances.data['instances'])
        except Exception:
            return False

    def _editable_strategy(self, strategy_type):
        self.idle()
        if self.automation.corrupt or self.strategy_instances.corrupt:
            raise PortalError('自动运行或策略实例状态无法核对，请先检查运行状态文件再修改策略代码。')
        if self.automation.data['enabled'] or any(x['enabled'] for x in self.strategy_instances.data['instances']) or self.strategy_instances.data['pending']:
            raise PortalError('停止自动运行和所有实例，并核对未完成批次后再修改策略代码。')

    def strategy_source(self, strategy_type, value=None):
        with self.lock:
            if value is None:
                return self.strategy_files.read(strategy_type)
            self._editable_strategy(strategy_type)
            result = self.strategy_files.write(strategy_type, value)
            self.clear_preview()
            return result

    def upload_strategy(self, content):
        with self.lock:
            self._editable_strategy(None)
            return self.strategy_files.install(content)

    def export_instance(self, ident, value):
        with self.lock:
            self.idle()
            if (not isinstance(value, dict) or not {'start', 'end', 'slippage_bps'} <= set(value)
                    or set(value) - {'start', 'end', 'slippage_bps', 'expected_strategy_type'}):
                raise PortalError('请填写回测区间和滑点。')
            item = self.strategy_instances.get(ident)
            check_expected_strategy(value.get('expected_strategy_type', _UNSPECIFIED_STRATEGY),
                                    item['strategy']['type'])
            cfg = load_config(self.root / 'configs/portfolio_config.json')
            cfg['strategy'] = deepcopy(item['strategy'])
            instance_file = self.folder / 'instance-configs' / (uuid4().hex + '.json')
            atomic_json(instance_file, cfg)
            experiment = 'exp-' + uuid4().hex[:16]
            try:
                export_backtest(instance_file, self.folder / 'experiments' / experiment,
                    start=value['start'], end=value['end'], slippage_bps=value['slippage_bps'],
                    note='策略实例：' + item['name'] + '。资金比例 ' + item['budget'] + '，本文件单独测试该策略。',
                    schedule=item['schedule'])
            finally:
                instance_file.unlink(missing_ok=True)
            return {'id': experiment, 'message': '已按此实例参数和计划导出 QuantConnect 回测代码。'}

    def strategy_instance_job(self, ident, action):
        with self.lock:
            self.idle()
            item = self.strategy_instances.get(ident)
            closing = {'strategy-reconcile', 'strategy-resolve', 'strategy-close-terminal'}
            cycle = self.strategy_instances.pending_cycle(ident) if action in closing else None
            if action in closing and cycle is None:
                raise PortalError('此实例没有未核对的账户批次。')
            if action not in {'strategy-check', 'strategy-preview'} | closing:
                raise PortalError('未知策略实例检查。')
            if action == 'strategy-preview' and item['broker_id'] in self.strategy_instances.data['pending']:
                raise PortalError('先核对未完成批次，再按已确认的持仓预览。')
            if action in {'strategy-resolve', 'strategy-close-terminal'} and any(x['enabled'] for x in self.strategy_instances.data['instances'] if x['broker_id'] == item['broker_id']):
                raise PortalError('先停止此账户的所有实例，再结束未提交余单。')
            return self._managed_job(action, ident=ident, cycle=cycle)

    def _managed_job(self, action, *, ident=None, cycle=None):
        job = dict(id=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8],
                   action=action, status='running', started_at=now(), message='检查策略运行…',
                   events=[], cancel_available=False, instance_id=ident)
        self.active = job
        def progress(message):
            with self.lock:
                job['message'] = str(message)[:800]
                job['events'] = [*job['events'], dict(at=now(), message=job['message'])][-60:]
        def worker(root, op, *args, **kwargs):
            return self.worker(root, op, *args, **kwargs)
        def work():
            try:
                if action == 'strategy-cycle':
                    result = self.strategy_instances.tick(worker, progress)
                elif action in {'strategy-check', 'strategy-preview'}:
                    item = deepcopy(self.strategy_instances.get(ident))
                    if action == 'strategy-preview':
                        item['_held_positions'] = deepcopy(self.strategy_instances.data['books'].get(
                            item['broker_id'], {}).get('owners', {}).get(ident, {}))
                        item['_selection_timeout_seconds'] = self.strategy_instances.data['limits']['selection_timeout_seconds']
                    result = worker(self.root, action, instance=item, progress=progress)
                    with self.lock:
                        self.strategy_instances.event(self.strategy_instances.get(ident), result['message'],
                                                      'error' if result['status'] != 'complete' else 'info')
                        self.strategy_instances.persist()
                else:
                    result = worker(self.root, action, cycle_path=cycle['path'], progress=progress)
                    with self.lock:
                        result = self.strategy_instances.finish_cycle(cycle, result)
            except Exception:
                read_only = action in {'strategy-check', 'strategy-preview'}
                result = {'status': 'blocked' if read_only else 'paused', 'message':
                    '本次只读检查未完成，请检查连接、数据和日志后重试。' if read_only else
                    '策略任务遇到异常，请先核对订单与归属。'}
                with self.lock:
                    if read_only:
                        self.strategy_instances.event(self.strategy_instances.get(ident), result['message'], 'error')
                        self.strategy_instances.persist()
                    else:
                        broker_id = cycle['broker_id'] if cycle else self.strategy_instances.active_broker_id
                        affected = {broker_id} if broker_id else {record['broker_id'] for record in
                            self.strategy_instances.data['instances'] if record['enabled']}
                        for broker_id in affected:
                            self.strategy_instances.pause_account(broker_id, result['message'])
            with self.lock:
                safe = self._public(result)
                job.update(status=safe['status'], message=safe['message'], result=safe, finished_at=now())
                self.jobs = [job, *self.jobs][:40]
                self._save_job(job)
                self.clear_preview()
                self.account = None
                self.broker_session.clear()
                self.active = None
        self.thread = threading.Thread(target=work, name='managed-strategy-worker', daemon=False)
        self.thread.start()
        return {'job_id': job['id']}

    def strategy_instances_poll(self):
        with self.lock:
            if not self.closing and not self.active and self.strategy_instances.due():
                self._managed_job('strategy-cycle')

    def change_strategy(self, strategy, expected_version):
        with self.lock:
            self.idle()
            cfg, version = self.config()
            if version != expected_version:
                raise PortalError('配置已被其他窗口修改，请刷新后重试。')
            if not isinstance(strategy, dict) or strategy.get('type') not in {
                    x['type'] for x in self.strategies()}:
                raise PortalError('请选择已注册的组合策略。')
            updated = deepcopy(cfg)
            updated['strategy'] = strategy
            self.registry.validate_strategy(updated)
            path = self.root / 'configs/portfolio_config.json'
            raw = read_json(path)
            raw['strategy'] = strategy
            normalize_config(dict(load_config(path), strategy=strategy))
            # Preserve broker, data, fees and risk limits exactly; back up the original.
            backup = self.folder / 'config-history' / (uuid4().hex + '.json')
            atomic_json(backup, read_json(path))
            if sha256(path.read_bytes()).hexdigest() != version:
                raise PortalError('保存前配置发生变化，请重新加载。')
            atomic_json(path, raw)
            self.clear_preview()
            self.selected_snapshot = None
            self.snapshot_cache.clear()
            return {'message': '策略已保存；请重新预览。'}

    def _register_snapshot(self, path, source, label=None):
        path = Path(path).resolve()
        if not path.is_relative_to((self.root / 'runs').resolve()) or not path.is_file():
            raise PortalError('数据文件不在本项目的运行目录。')
        stat = path.stat()
        if stat.st_size > MAX_FILE:
            raise PortalError('数据文件超过 32 MB。')
        key = sha256((source + str(path)).encode()).hexdigest()[:24]
        cached = self.snapshot_cache.get(key)
        _, entry = self.registry.resolve('data', source)
        stamp = (stat.st_mtime_ns, stat.st_size, json.dumps(entry, sort_keys=True), self.registry.version())
        if not cached or cached[0] != stamp:
            raw = read_json(path)
            metadata = self.registry.control('data', source).metadata(raw)
            meta = dict(metadata, id=key, source=source, name=label or path.parent.name, size=stat.st_size)
            self.snapshot_cache[key] = (stamp, meta)
        self.snapshots[key] = path
        return deepcopy(self.snapshot_cache[key][1])

    def _sources(self):
        items = []
        for descriptor in self.registry.descriptors('data'):
            source = descriptor['id']
            item = dict(descriptor, snapshot=None, issue=None)
            if descriptor['enabled']:
                try:
                    adapter = self.registry.control('data', source)
                    item.update(adapter.configuration_status())
                    pointer = adapter.directory / 'latest.json'
                    if pointer.is_file():
                        path = Path(read_json(pointer)['path'])
                        if not path.is_absolute():
                            path = self.root / path
                        if not path.resolve().is_relative_to(adapter.directory.resolve()):
                            raise PortalError('数据索引指向当前数据模块的目录外。')
                        item['snapshot'] = self._register_snapshot(path, source)
                except Exception:
                    item['issue'] = '当前策略需求与已有数据不匹配，或模块配置无法读取；请检查后重新采集。'
            items.append(item)
        return items

    def select_source(self, source):
        with self.lock:
            self.idle()
            descriptor = self.registry.descriptor('data', source)
            if not descriptor['enabled']:
                raise PortalError(descriptor['reason'])
            self.source = source
            self.selected_snapshot = None
            self.clear_preview()
            self._save_preferences()
            return {'message': '已切换数据源。'}

    def _save_preferences(self):
        atomic_json(self.folder / 'preferences.json', {'source': self.source, 'broker': self.broker_id})

    def select_broker(self, broker_id):
        with self.lock:
            self.idle()
            descriptor = self.registry.descriptor('broker', broker_id)
            if not descriptor['enabled']:
                raise PortalError(descriptor['reason'])
            if broker_id == self.broker_id:
                return {'message': '当前券商模块保持不变。'}
            self.switch_issues()
            self.disconnect_authorization()
            self.broker_id = broker_id
            self.monitor.next_observation = 0
            self.monitor.last_check = self.monitor.last_success = self.monitor.error = None
            self.account = None
            self.clear_preview()
            self._save_preferences()
            return {'message': '券商模块已切换，请重新检查账户与预览。'}

    def switch_issues(self):
        details = (self.account or {}).get('details', {})
        if details.get('open_order_count', 0):
            raise PortalError('旧账户存在未完成委托，请先在券商端核对后再切换。')
        path = self.registry.control('broker', self.broker_id).journal_path
        try:
            if JournalReader(path).has_unresolved():
                raise PortalError('原账户账本仍有待核对的委托，请先完成状态核对。')
        except JournalReadError as exc:
            raise PortalError(str(exc)) from None

    @property
    def authorization(self):
        return self.authorizations.current

    @authorization.setter
    def authorization(self, value):
        self.authorizations.current = value

    def _authorization(self):
        return self.authorizations.provider(self.broker_id)

    def begin_authorization(self, callback):
        with self.lock:
            self.idle()
            self.switch_issues()
            self.broker_session.clear()
            self.account = None
            self.clear_preview()
            self.authorization_notice = None
            return self._authorization().begin(callback)

    def finish_authorization(self, state, code):
        with self.lock:
            self.idle()
            if self.authorization is None:
                raise PortalError('授权请求已失效，请从当前券商模块重新开始。')
            try:
                self.authorization.finish(state, code)
                self.authorization_notice = '网页授权完成，请选择返回的账户并检查连接。'
            except ControlError:
                self.authorization_notice = '网页授权未完成，请重新开始并仅授权账户读取。'
                raise

    def bind_authorization(self, ident):
        with self.lock:
            self.idle()
            self.switch_issues()
            result = self._authorization().bind(ident)
            self.broker_session.clear()
            self.account = None
            self.clear_preview()
            return result

    def authorization_state(self):
        if self.registry.descriptor('broker', self.broker_id).get('authentication', {}).get('mode') != 'oauth':
            return None
        return dict(self._authorization().public(), notice=self.authorization_notice)

    def disconnect_authorization(self):
        self.authorizations.disconnect()
        self.broker_session.clear()
        self.account = None
        self.clear_preview()

    def disconnect_broker(self):
        with self.lock:
            self.idle()
            self.switch_issues()
            if self.automation.data['enabled']:
                self.automation.disable()
            self.disconnect_authorization()
            self.monitor.configure({'enabled':False})
            return {'message': '当前账户在本程序中已停用；官方客户端登录和券商端授权需分别退出或撤销。'}

    def snapshot(self, sources=None):
        if self.selected_snapshot:
            try:
                return self._register_snapshot(self.snapshots[self.selected_snapshot], self.source, '导入的快照')
            except (ValueError, OSError, KeyError, TypeError):
                # Also invalidate uploads when configuration was edited outside the Portal.
                self.selected_snapshot = None
                self.clear_preview()
                for item in sources or []:
                    if item['id'] == self.source:
                        item['issue'] = '已导入的快照不再满足当前策略需求，请重新采集或导入。'
                return None
        return next((x.get('snapshot') for x in (sources or self._sources()) if x['id'] == self.source), None)

    def upload_snapshot(self, content):
        with self.lock:
            self.idle()
            if 'upload' not in self.registry.descriptor('data', self.source)['capabilities']:
                raise PortalError('此数据模块不支持文件导入。')
            if len(content) > MAX_FILE:
                raise PortalError('快照不能超过 32 MB。')
            path = self.folder / 'snapshots' / (uuid4().hex + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            try:
                meta = self._register_snapshot(path, self.source, '导入的快照')
            except (ValueError, KeyError, TypeError):
                path.unlink(missing_ok=True)
                raise PortalError('无法读取该快照，请导入项目支持的 JSON 数据。') from None
            self.selected_snapshot = meta['id']
            self.clear_preview()
            return meta

    def orders(self):
        with self.lock:
            control = self.registry.control('broker', self.broker_id)
            path = control.journal_path
            # Connector construction supplies the canonical filename only; no
            # settings, account binding, broker inspection or session is opened.
            connector = control.connector()
            ledger_path = control.state_dir / connector.state_files[0]
        try:
            return JournalReader(path, broker_ledger_path=ledger_path,
                                 envelope_dir=self.folder / 'execution').recent()
        except JournalReadError as exc:
            return {'items': [], 'message': str(exc)}

    def performance(self):
        with self.lock:
            try:
                scope = self.monitor.scope(self.broker_id)
                report = self.monitor.performance.report(scope)
                try:
                    orders = self.orders()['items']
                    for cycle in report['cycles']:
                        cycle['fills'] = [o for o in orders if o.get('cycle') == cycle['decision_id']]
                except (ValueError, OSError):
                    pass
                report['broker_id'] = self.broker_id
                report['account_key'] = sha256(scope.encode()).hexdigest()
                report['view_token'] = self._performance_token(scope,report.get('latest'))
                return self._public(report)
            except ControlError as exc:
                return dict(latest=None,curve=[],cycles=[],positions=[],cash_events=[],metrics={},
                            message=str(exc), broker_id=self.broker_id)

    def performance_candles(self, interval='1h', limit=96, account_key=None):
        from framework.account_candles import validate_period
        try:
            validate_period(interval, limit)
        except ValueError:
            raise PortalError('K 线周期或数量无效。') from None
        with self.lock:
            try:
                scope = self.monitor.scope(self.broker_id)
            except ControlError as exc:
                return dict(bars=[], interval=interval, broker_id=self.broker_id, message=str(exc))
            current_key = sha256(scope.encode()).hexdigest()
            if account_key is not None and account_key != current_key:
                raise PortalError('账户绑定已变化，请刷新账户表现。')
            report = self.monitor.performance.candles(scope, interval, limit)
            report.update(broker_id=self.broker_id, account_key=current_key,
                          interval_seconds=self.monitor.settings.config['interval_seconds'])
            return self._public(report)

    def performance_cash_event(self, value):
        with self.lock:
            value = dict(value)
            scope = self._check_performance_token(value.pop('view_token',None))
            self.monitor.performance.cash_event(scope,value)
            return {'message':'资金事件已记录，请重新核对统计区间。'}

    def performance_review(self, value):
        if set(value) != {'through','view_token'}:
            raise PortalError('请核对最新账户记录。')
        with self.lock:
            scope = self._check_performance_token(value['view_token'])
            self.monitor.performance.review_cash(scope,value['through'])
            return {'message':'已记录本次资金流水核对。'}

    def _performance_token(self, scope, latest):
        return sha256((scope+self.broker_id+str((latest or {}).get('as_of'))).encode()).hexdigest()

    def _check_performance_token(self, token):
        scope = self.monitor.scope(self.broker_id)
        latest = self.monitor.performance.report(scope).get('latest')
        if not latest or token != self._performance_token(scope,latest):
            raise PortalError('账户或观测记录已变化，请刷新后重新核对。')
        return scope

    def performance_remove_event(self,value):
        if set(value) != {'id','view_token'}:
            raise PortalError('资金记录参数无效。')
        with self.lock:
            scope = self._check_performance_token(value['view_token'])
            self.monitor.performance.remove_cash_event(scope,value['id'])
            return {'message':'错误记录已移除，请重新核对统计区间。'}

    def monitoring_configure(self, value):
        with self.lock:
            return self.monitor.configure(value)

    def _record_observation(self, action, broker, result, scope=None):
        # Reporting failure must not change an already confirmed execution result.
        try:
            if action == 'observe':
                self.monitor.observed(broker,result,scope)
            if (result.get('decision_id') and 'targets' in result
                    and 'observe' in self.registry.descriptor('broker',broker)['capabilities']):
                self.monitor.cycle(broker,result,self.config()[0]['strategy'])
            if action in {'execute','recover','reconcile'}:
                self.monitor.next_observation = 0
        except Exception:
            self.monitor.error = '账户表现记录未通过校验，已有交易结果与历史记录已保留。'
            self.monitor.emit('reporting:'+broker+now(),self.monitor.error,'error','positions')

    def monitoring_poll(self):
        with self.lock:
            if self.closing:
                return
            self.monitor.automation_events(list(self.automation.data['events']))
            self.monitor.pump()
            if self.active or not self.monitor.settings.config['enabled'] or time.time() < self.monitor.next_observation:
                return
            self.monitor.next_observation = time.time()+self.monitor.settings.config['interval_seconds']
            try:
                if 'observe' not in self.registry.descriptor('broker',self.broker_id)['capabilities']:
                    return
                self.start('observe')
            except Exception:
                self.monitor.error = '尚未取得账户监控数据，请检查所选券商配置。'
                self.monitor.events.transition('monitor-config:'+self.broker_id,False,self.monitor.error,'broker',
                    notify=self.monitor.settings.config['channel']!='none')

    def state(self):
        with self.lock:
            self.automation.validate_binding(self.broker_id, self.source)
            cfg, version = self.config()
            sources = self._sources()
            strategies = self.strategies()
            selected_strategy = next((s for s in strategies if s['type'] == cfg['strategy']['type']), None)
            strategy_issue = None if selected_strategy and selected_strategy['enabled'] else '当前策略不可用，请选择可用策略或修复其依赖。'
            brokers = self.registry.descriptors('broker')
            broker = next((b for b in brokers if b['id'] == self.broker_id),
                          self.registry.unavailable(self.broker_id, '当前券商登记已不存在。'))
            source = next((s for s in sources if s['id'] == self.source),
                          self.registry.unavailable(self.source, '当前数据源登记已不存在。'))
            if not any(s['id'] == self.source for s in sources):
                sources.append(source)
            context_issue = None
            try:
                context = self.registry.context(self.broker_id, self.source)
            except Exception:
                context = None
                context_issue = '当前模块组合无法加载，请检查不可用模块及配置。'
            if self.preview and (context is None or self.preview_context != context):
                self.clear_preview()
            compatibility = None
            try:
                self.registry.compatible(cfg, self.source)
            except (ControlError, KeyError):
                compatibility = '当前数据模块无法满足策略要求，请检查模块兼容性。'
            try:
                requirements = self.registry.data_requirements(cfg).describe()
            except ControlError:
                requirements = None
            verified, authorization = broker['mode'] == 'paper', None
            if broker['enabled']:
                try:
                    verified = verified or self.broker_session.valid(self.broker_id, self.registry.connection_context(self.broker_id))
                    authorization = self.authorization_state()
                except Exception:
                    verified = False
                    broker = dict(broker, reason='账户配置或授权无法读取，请重新检查连接。')
            snapshot = self.snapshot(sources)
            actions = operation_states(broker=broker, source=source, snapshot=snapshot, preview=self.preview,
                verified=verified, account=self.account, preview_authorized=bool(self.preview_private),
                busy=bool(self.active), closing=self.closing, compatibility_issue=compatibility,
                context_issue=context_issue, strategy_issue=strategy_issue, authorization=authorization)
            if self.automation.data['enabled']:
                actions['execute'] = {'enabled': False, 'blockers': [], 'message': '自动运行已开启；手动下单前请先关闭自动运行。', 'next_action': None}
                for action in ('resolve-remainder', 'close-terminal'):
                    actions[action] = {'enabled': False, 'blockers': [], 'message': '请先关闭自动运行，再处理未提交余单。', 'next_action': None}
            managed = self.managed_account(self.broker_id)
            if managed:
                for action in ('execute', 'resolve-remainder', 'close-terminal'):
                    actions[action] = {'enabled': False, 'blockers': [], 'message': '此账户由策略实例管理，请在「策略运行」中操作。', 'next_action': None}
            return self._public({'started_at': self.started_at, 'mode': broker['mode'],
                'execution_scope': load_execution_policy(self.root).public(),
                'execution_context': broker.get('execution_context'),
                'market_session': selected_market_session(broker, at=datetime.fromisoformat(now()),
                    current_context=context,
                    automation=self.automation.data if not self.automation.corrupt else None,
                    instances=self.strategy_instances.data if not self.strategy_instances.corrupt else None,
                    connection_context=lambda: self.registry.connection_context(self.broker_id)),
                'schedule': 'automatic' if self.automation.data['enabled'] else 'manual',
                'automation': self.automation.public(self.broker_id, self.source),
                'monitoring': self.monitor.public(),
                'strategy_manager': self.strategy_instances.public(),
                'managed_account': managed,
                'data_requirements': requirements, 'broker': broker, 'brokers': brokers,
                'inventory': self.registry.inventory(), 'compatibility_issue': compatibility,
                'module_issue': context_issue, 'actions': actions,
                'source': self.source, 'sources': sources, 'snapshot': snapshot,
                'strategy': cfg['strategy'], 'strategy_name': selected_strategy['name'] if selected_strategy else cfg['strategy']['type'],
                'config_version': version, 'limits': cfg['limits'], 'backtest_config': cfg.get('backtest'),
                'strategies': strategies, 'account': self.account, 'broker_authorization': authorization,
                'broker_verified': verified, 'preview': self.preview, 'active': self.active, 'jobs': self.jobs[:30],
                'preflight_job': next((j for j in self.jobs if j.get('action') == 'preflight'
                    and j.get('broker') == self.broker_id and j.get('source') == self.source
                    and j.get('context_version') == context), None)})

    def _save_job(self, job):
        atomic_json(self.folder / 'jobs' / (job['id'] + '.json'), self._public(job))
        if job.get('finished_at') and job['action'] != 'observe':
            failed = job['status'] in {'blocked','paused','interrupted'}
            if failed or job['action'] in {'refresh','execute','reconcile','automation','strategy-cycle','strategy-reconcile','strategy-resolve','strategy-close-terminal','close-terminal'}:
                self.monitor.emit('job:'+job['id'],job['message'],'error' if failed else 'info','logs',at=job['finished_at'])

    def start(self, action, credentials=None):
        with self.lock:
            self.idle()
            if action not in WORKBENCH_ACTIONS:
                raise PortalError('未知操作。')
            if action == 'preflight':
                from app.preflight import start_preflight
                return start_preflight(self)
            view = self.state()
            allowed = view['actions'][action]
            if not allowed['enabled']:
                raise PortalError(allowed['message'])
            source = self.source
            broker_id = self.broker_id
            source_spec = next(s for s in view['sources'] if s['id'] == source)
            secret_values = tuple(str(credentials[f['name']]) for f in source_spec.get('fields', [])
                                  if f.get('secret') and credentials and credentials.get(f['name']))
            broker_spec = view['broker']
            binding = self.registry.connection_context(broker_id) if action != 'configure-data' else None
            if action in {'prepare', 'execute'} and broker_spec['mode'] != 'paper':
                try:
                    self.broker_session.require(broker_id, binding)
                except ControlError:
                    self.clear_preview()
                    raise
            if action in {'status', 'offline'} and 'inspect' not in broker_spec['capabilities']:
                raise PortalError('此账户模块未声明连接检查能力。')
            if action == 'execute' and 'execute' not in broker_spec['capabilities']:
                raise PortalError('此账户模块未声明执行能力。')
            if action == 'prepare' and 'prepare' not in broker_spec['capabilities']:
                raise PortalError('当前账户模块没有订单准备能力。')
            context = context_for(self.registry, action, broker_id, source)
            financial_scope = self.monitor.scope(broker_id) if action == 'observe' else None
            operation_cap = DATA_CAPABILITIES
            if action in operation_cap and operation_cap[action] not in source_spec['capabilities']:
                raise PortalError('所选数据模块没有声明这项能力。')
            snapshot_path = token = None
            if action in {'preview', 'prepare'}:
                snap = self.snapshot()
                if not snap:
                    raise PortalError('请先下载或导入数据。')
                snapshot_path = self.snapshots[snap['id']]
                self.clear_preview()
            if action == 'refresh':
                self.selected_snapshot = None
                self.clear_preview()
            if action == 'execute':
                if not self.preview_private or not self.preview or not self.preview.get('execution_ready'):
                    raise PortalError('没有可执行的预览；研究数据不能提交订单。')
                if self.preview_context != context:
                    self.clear_preview()
                    raise PortalError('模块或配置已经变化，请重新预览。')
                if broker_spec['mode'] != 'paper' and (not self.account or self.account.get('status') != 'connected'):
                    raise PortalError('请先检查当前账户模块的连接。')
                snapshot_path, token = self.preview_private
                self.clear_preview()  # One click consumes the preview, even on failure.
            job = {'id': datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8],
                   'action': action, 'source': source, 'source_name': source_spec['name'],
                   'broker': broker_id, 'broker_name': broker_spec['name'], 'status': 'running', 'started_at': now(),
                   'message': '正在处理…', 'events': [], 'cancel_available': False}
            self.active = job
            self.jobs.insert(0, job)
            self.jobs = self.jobs[:40]
            self._save_job(job)

            def progress(message):
                with self.lock:
                    job['message'] = self._public(str(message), secret_values)[:800]
                    job['events'] = (job['events'] + [{'at': now(), 'message': job['message']}])[-60:]
                    job['cancel_available'] = action == 'refresh' and 'cancel' in source_spec['capabilities']
                    self._save_job(job)

            def work():
                try:
                    result = self.worker(self.root, action, snapshot_path, token, progress,
                                         source_id=source, broker_id=broker_id, expected_context=context, credentials=credentials)
                except Exception as exc:
                    # Vendor exceptions can contain credentials. Never log their raw text.
                    result = dict(failure_details(exc, action, fallback='操作未取得有效结果，请检查本地配置；程序不会自动重试。'),
                                  status='paused' if action == 'execute' else 'blocked')
                finally:
                    if credentials is not None:
                        credentials.clear()
                with self.lock:
                    self._record_observation(action,broker_id,result,financial_scope)
                    safe_result = self._public(result, secret_values)
                    job.update(status=safe_result['status'], message=safe_result['message'], finished_at=now(),
                               result=safe_result, cancel_available=False)
                    if action == 'status':
                        self.account = dict(safe_result, checked_at=now())
                        self.broker_session.clear()
                        try:
                            unchanged = self.registry.connection_context(broker_id) == binding
                        except (ValueError, OSError, KeyError, TypeError):
                            unchanged = False
                        if safe_result['status'] == 'connected' and unchanged:
                            self.broker_session.verify(broker_id, binding)
                        elif not unchanged:
                            self.account = None
                            job.update(status='blocked', message='检查期间账户配置改变，请重新检查连接。')
                    if action in {'preview', 'prepare', 'refresh'} and result.get('status') == 'previewed':
                        try:
                            still_current = context_for(self.registry, action, broker_id, source) == context
                        except (ValueError, OSError, KeyError):
                            still_current = False
                        if still_current:
                            self.preview = dict(safe_result, created_at=now(), source=source, broker=broker_id)
                            self.preview_context = context
                            execution_path = result.get('snapshot_path') or snapshot_path
                            if result.get('execution_ready') and result.get('preview_token') and execution_path:
                                self.preview_private = (Path(execution_path), result['preview_token'])
                                if action == 'prepare' and safe_result.get('account_details'):
                                    self.account = {'status':'connected','message':'准备时已核对绑定的账户。',
                                                    'details':safe_result['account_details'],
                                                    'checked_at':now()}
                    if action == 'execute':
                        self.account = None  # Do not display pre-trade holdings as current.
                    if action in {'reconcile', 'resolve-remainder', 'close-terminal'}:
                        self.automation.reconciled(broker_id, result)
                    self.active = None
                    self._save_job(job)

            self.thread = threading.Thread(target=work, name='portal-worker', daemon=False)
            self.thread.start()
            return {'job_id': job['id']}

    def cancel_download(self):
        with self.lock:
            if not self.active or not self.active.get('cancel_available'):
                raise PortalError('当前没有可停止的下载。')
            # Collector announces progress after removing the previous cancellation file.
            self.registry.control('data', self.active['source']).cancel()
            if self.active['action'] == 'preflight':
                self.active['cancel_requested'] = True
            self.active['cancel_available'] = False
            self.active['message'] = '已请求停止，等待当前数据请求结束。'
            self._save_job(self.active)
            return {'message': self.active['message']}

    def automation_configure(self, value):
        with self.lock:
            self.idle()
            return self.automation.configure(value)

    def automation_enable(self):
        with self.lock:
            self.idle()
            if self.managed_account(self.broker_id):
                raise PortalError('此账户由策略实例管理，请在「策略运行」启动或停止实例。')
            verified = self.broker_session.valid(self.broker_id, self.registry.connection_context(self.broker_id))
            self.clear_preview()
            result = self.automation.enable(self.broker_id, self.source, verified, initialize=True)
            self.scheduler_wake.set()
            return result

    def automation_disable(self):
        # Intentionally allowed during a job. Workers re-read revocation before new submissions.
        with self.lock:
            return self.automation.disable()

    def start_scheduler(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return
        self.scheduler_stop.clear()
        def loop():
            while not self.scheduler_stop.is_set():
                try:
                    self.strategy_instances_poll()
                    self.automation_poll()
                except Exception:
                    # Never allow the scheduling thread to die silently or log SDK secrets.
                    with self.lock:
                        if not self.automation.corrupt:
                            self.automation.pause('自动服务遇到异常并已暂停，请查看运行记录。')
                try:
                    self.monitoring_poll()
                except Exception:
                    self.monitor.error = '监控服务暂不可用，请检查运行日志。'
                self.scheduler_wake.wait(5)
                self.scheduler_wake.clear()
        self.scheduler_thread = threading.Thread(target=loop, name='paper-scheduler', daemon=True)
        self.scheduler_thread.start()

    def stop_scheduler(self):
        self.scheduler_stop.set()
        self.scheduler_wake.set()
        if self.scheduler_thread:
            self.scheduler_thread.join(6)

    def automation_poll(self):
        with self.lock:
            if self.closing or self.active or not self.automation.due():
                return
            self.automation.validate_binding(self.broker_id, self.source)
            if not self.automation.data['enabled']:
                return
            binding = self.automation.data['binding']
            job = dict(id=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8],
                       action='automation', source=binding['source_id'], source_name=binding['source_name'],
                       broker=binding['broker_id'], broker_name=binding['broker_name'], status='running',
                       started_at=now(), message='检查自动计划…', events=[], cancel_available=False)
            self.active = job

            def progress(message):
                with self.lock:
                    job['message'] = str(message)[:800]
                    job['events'] = (job['events']+[dict(at=now(),message=job['message'])])[-60:]

            def work():
                result = None
                try:
                    def observed_worker(root, action, *args, **kwargs):
                        value = self.worker(root,action,*args,**kwargs)
                        with self.lock:
                            self._record_observation(action,binding['broker_id'],value)
                        return value
                    result = self.automation.tick(observed_worker, progress)
                except Exception as exc:
                    result = dict(failure_details(exc, 'automation', fallback='自动任务未完成，已停止新委托；请先核对订单。'), status='paused')
                    if not self.automation.corrupt:
                        self.automation.pause(result['message'])
                finally:
                    with self.lock:
                        if result is not None:
                            safe_result = self._public(result)
                            job.update(status=safe_result['status'], message=safe_result['message'],
                                       result=safe_result, finished_at=now())
                            self.jobs = [job, *self.jobs][:40]
                            self._save_job(job)
                            self.clear_preview()
                            self.account = None
                            self.broker_session.clear()
                            if safe_result.get('startup_check') and self.registry.context(self.broker_id, self.source) == binding['context']:
                                selection = safe_result.get('selection')
                                if selection:
                                    self.preview = dict(selection, execution_ready=False, created_at=now(),
                                                        source=self.source, broker=self.broker_id)
                                    self.preview_context = binding['context']
                                account = safe_result.get('account_check')
                                if account and account.get('status') == 'connected':
                                    self.account = dict(account, checked_at=now())
                                    self.broker_session.verify(self.broker_id, self.registry.connection_context(self.broker_id))
                        self.active = None
            self.thread = threading.Thread(target=work, name='automatic-paper-worker', daemon=False)
            self.thread.start()

    def _experiment(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'exp-[0-9a-f]{16}', ident):
            raise PortalError('实验编号无效。')
        path = self.folder / 'experiments' / ident
        if not path.is_dir() or not path.resolve().is_relative_to(self.folder.resolve()):
            raise PortalError('实验不存在。')
        return path

    def experiments(self):
        items = []
        for path in sorted((self.folder / 'experiments').glob('exp-*'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            try:
                manifest = validate_export(path)
                entry = {'id': path.name, 'configuration': manifest['configuration'],
                         'note': manifest.get('research_note', ''), 'results': []}
                for result in sorted(path.glob('result-*')):
                    try:
                        record = verified_record(result)
                        if parse_result(read_json(result / 'raw-result.json')) != record['result']:
                            raise ValueError('Changed summary')
                        entry['results'].append({'id': path.name + '/' + result.name,
                                                 'metrics': record['result']['metrics'],
                                                 'completion': record['result']['completion'],
                                                 'code_evidence': record['code_evidence'],
                                                 'warnings': record['warnings']})
                    except (ValueError, OSError, KeyError):
                        entry['results'].append({'id': path.name + '/' + result.name, 'invalid': True})
                items.append(entry)
            except (ValueError, OSError, KeyError):
                items.append({'id': path.name, 'invalid': True, 'results': []})
        return items

    def export(self, start, end, slippage_bps, note='', *, expected_strategy_type=_UNSPECIFIED_STRATEGY):
        with self.lock:
            self.idle()
            cfg, _ = self.config()
            check_expected_strategy(expected_strategy_type, cfg['strategy']['type'])
            ident = 'exp-' + uuid4().hex[:16]
            out = self.folder / 'experiments' / ident
            # Export exactly the configuration checked under this lock. A local
            # file edit after the check must not change the exported strategy.
            config_file = self.folder / ('export-config-' + uuid4().hex + '.json')
            atomic_json(config_file, cfg)
            try:
                export_backtest(config_file, out, start=start, end=end,
                                slippage_bps=slippage_bps, note=note, schedule=self.automation.data['config'])
            finally:
                config_file.unlink(missing_ok=True)
            return {'id': ident, 'message': '代码已导出；请在 QuantConnect 中运行。'}

    def import_backtest(self, ident, content):
        with self.lock:
            self.idle()
            if len(content) > MAX_FILE:
                raise PortalError('回测 JSON 不能超过 32 MB。')
            experiment = self._experiment(ident)
            path = self.folder / ('upload-' + uuid4().hex + '.json')
            path.write_bytes(content)
            try:
                out = import_result(experiment, path, parse_result)
            except FileExistsError:
                raise PortalError('该结果已经导入。') from None
            finally:
                path.unlink(missing_ok=True)
            return {'id': ident + '/' + out.name, 'message': '结果已导入；未提供运行代码，代码来源尚未核验。'}

    def compare(self, identifiers):
        with self.lock:
            self.idle()
            if not isinstance(identifiers, list) or not 2 <= len(identifiers) <= 10:
                raise PortalError('请选择 2–10 份回测结果。')
            folders = []
            for ident in identifiers:
                if not isinstance(ident, str) or not re.fullmatch(r'exp-[0-9a-f]{16}/result-[0-9a-f]{16}', ident):
                    raise PortalError('回测结果编号无效。')
                folders.append(self._experiment(ident.split('/')[0]) / ident.split('/')[1])
            return compare_results(folders, parse_result, self.folder / 'comparisons' / uuid4().hex)
