"""Portal state and jobs. No trading logic or vendor SDK lives in the UI layer."""
from copy import deepcopy
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from backtests.experiments import (compare_results, import_result, read_json,
                                  validate_export, verified_record)
from backtests.replay import parse_any, replay
from bot import export_backtest
from app.assembly import build_portfolio_strategy
from data.snapshot import load_snapshot
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
from app.logs import incident_id, log_exception
from app.atomic import replace_file
from app.backfill import Backfill, CADENCES, MAX_DATES, covered, estimate_requests, schedule_dates
from app.instance_attribution import attribute

MAX_FILE = 32 * 1024 * 1024
PortalError = ControlError
_UNSPECIFIED_STRATEGY = object()
log = logging.getLogger('trading_bot.workbench')


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
        with temporary.open('w', encoding='utf-8') as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(temporary, path)
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
        self.backfill = Backfill(self.root)
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
        # Runs on every progress message: read the manifests, do not build descriptors.
        return public(value, self.registry.secret_fields(), secret_values)

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

    def instance_attribution(self, ident):
        """Valuation and P&L of one instance from its share of the broker's book."""
        with self.lock:
            item = self.strategy_instances.get(ident)
            broker_id = item['broker_id']
            cycles = self.strategy_instances.cycle_history(broker_id)
            observations = []
            try:
                observations = self.monitor.performance.positions_history(self.monitor.scope(broker_id))
            except (ControlError, ValueError, OSError):
                pass  # no observation capability or history: value stays unknown, not zero
            try:
                fills = self.orders(broker_id).get('items', [])
            except (ControlError, ValueError, OSError):
                fills = []
        return self._public(attribute(ident, cycles, observations, fills))

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
                try:
                    safe = self._public(result)
                    if action == 'strategy-cycle' and safe.get('idle') is True:
                        # Nothing was due: no job record, no event, and the manual
                        # account verification stays intact. Polls happen every 30 s.
                        return
                    job.update(status=safe['status'], message=safe['message'], result=safe, finished_at=now())
                    self.jobs = [job, *self.jobs][:40]
                    self._save_job(job)
                    self.clear_preview()
                    self.account = None
                    self.broker_session.clear()
                except Exception as exc:
                    self._finalization_failed(job, exc)
                finally:
                    # Whatever happens while recording the result, the single-flight
                    # slot is released; a stuck 'active' would freeze every poll.
                    if self.active is job:
                        self.active = None
        self.thread = threading.Thread(target=work, name='managed-strategy-worker', daemon=False)
        self.thread.start()
        return {'job_id': job['id']}

    def _finalization_failed(self, job, exc):
        """Record (never raise) a failure while finishing a job; the caller releases the slot."""
        ident = incident_id()
        log_exception(log, f"job finalization {job.get('action')}", exc, ident)
        job.update(status='interrupted', finished_at=now(),
                   message=f'任务已结束，但记录结果时出现异常，请核对运行记录。（诊断编号 {ident}）')
        try:
            if job not in self.jobs:
                self.jobs = [job, *self.jobs][:40]
            self._save_job(job)
        except Exception:
            pass

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

    def configure_broker(self, value):
        from app.broker_settings import configure_broker
        with self.lock:
            return configure_broker(self, value)

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
            if any(item['enabled'] and item['broker_id'] == self.broker_id
                   for item in self.strategy_instances.data.get('instances', [])):
                raise PortalError('此账户仍有已开启的策略实例，请先停止实例再断开账户。')
            notes = []
            if self.automation.data['enabled']:
                self.automation.disable()
                notes.append('单策略计划已关闭')
            self.disconnect_authorization()
            if self.monitor.settings.config['enabled']:
                # Pause observation of this account without discarding queued
                # notifications (other accounts' alerts stay in the outbox).
                self.monitor.settings.save({'enabled': False})
                self.monitor.next_observation = 0
                notes.append('账户定时监控已关闭')
            self.monitor.error = self.monitor.unconfigured = None
            tail = ('；' + '，'.join(notes)) if notes else ''
            return {'message': f'当前账户在本程序中已停用{tail}；官方客户端登录和券商端授权需分别退出或撤销。'}

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

    def view_broker(self, broker_id=None):
        """Resolve the account a read-only view refers to.

        The account page may look at any registered account's ledger and
        performance history without changing the operating account (which owns
        previews, authorization and the single-strategy plan binding).
        """
        if broker_id in (None, '', self.broker_id):
            return self.broker_id
        if not isinstance(broker_id, str) or not any(item['id'] == broker_id for item in self.registry.descriptors('broker')):
            raise PortalError('要查看的账户不存在。')
        return broker_id

    def orders(self, broker_id=None):
        with self.lock:
            broker_id = self.broker_id if broker_id in (None, '') else self.view_broker(broker_id)
            control = self.registry.control('broker', broker_id)
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

    def performance(self, broker_id=None):
        with self.lock:
            viewed = broker_id if isinstance(broker_id, str) and broker_id else self.broker_id
            try:
                viewed = self.view_broker(broker_id)
                scope = self.monitor.scope(viewed)
                report = self.monitor.performance.report(scope)
                try:
                    orders = self.orders(viewed)['items']
                    for cycle in report['cycles']:
                        cycle['fills'] = [o for o in orders if o.get('cycle') == cycle['decision_id']]
                except (ValueError, OSError):
                    pass
                report['broker_id'] = viewed
                report['account_key'] = sha256(scope.encode()).hexdigest()
                report['view_token'] = self._performance_token(scope, report.get('latest'), viewed)
                return self._public(report)
            except ControlError as exc:
                return dict(latest=None,curve=[],cycles=[],positions=[],cash_events=[],metrics={},
                            message=str(exc), broker_id=viewed)

    def performance_candles(self, interval='1h', limit=96, account_key=None, broker_id=None):
        from framework.account_candles import validate_period
        try:
            validate_period(interval, limit)
        except ValueError:
            raise PortalError('K 线周期或数量无效。') from None
        with self.lock:
            viewed = self.view_broker(broker_id)
            try:
                scope = self.monitor.scope(viewed)
            except ControlError as exc:
                return dict(bars=[], interval=interval, broker_id=viewed, message=str(exc))
            current_key = sha256(scope.encode()).hexdigest()
            if account_key is not None and account_key != current_key:
                raise PortalError('账户绑定已变化，请刷新账户表现。')
            report = self.monitor.performance.candles(scope, interval, limit)
            report.update(broker_id=viewed, account_key=current_key,
                          interval_seconds=self.monitor.settings.config['interval_seconds'])
            return self._public(report)

    def performance_cash_event(self, value):
        with self.lock:
            value = dict(value)
            viewed = value.pop('broker', None)
            scope = self._check_performance_token(value.pop('view_token',None), viewed)
            self.monitor.performance.cash_event(scope,value)
            return {'message':'资金事件已记录，请重新核对统计区间。'}

    def performance_review(self, value):
        if not {'through','view_token'} <= set(value) or set(value) - {'through','view_token','broker'}:
            raise PortalError('请核对最新账户记录。')
        with self.lock:
            scope = self._check_performance_token(value['view_token'], value.get('broker'))
            self.monitor.performance.review_cash(scope,value['through'])
            return {'message':'已记录本次资金流水核对。'}

    def _performance_token(self, scope, latest, broker_id=None):
        return sha256((scope+(broker_id or self.broker_id)+str((latest or {}).get('as_of'))).encode()).hexdigest()

    def _check_performance_token(self, token, broker_id=None):
        viewed = self.view_broker(broker_id)
        scope = self.monitor.scope(viewed)
        latest = self.monitor.performance.report(scope).get('latest')
        if not latest or token != self._performance_token(scope, latest, viewed):
            raise PortalError('账户或观测记录已变化，请刷新后重新核对。')
        return scope

    def performance_remove_event(self,value):
        if not {'id','view_token'} <= set(value) or set(value) - {'id','view_token','broker'}:
            raise PortalError('资金记录参数无效。')
        with self.lock:
            scope = self._check_performance_token(value['view_token'], value.get('broker'))
            self.monitor.performance.remove_cash_event(scope,value['id'])
            return {'message':'错误记录已移除，请重新核对统计区间。'}

    def monitoring_configure(self, value):
        with self.lock:
            return self.monitor.configure(value)

    def _record_observation(self, action, broker, result, scope=None):
        # Reporting failure must not change an already confirmed execution result.
        notify = self.monitor.settings.config['channel'] != 'none'
        try:
            if action == 'observe':
                self.monitor.observed(broker,result,scope)
            if (result.get('decision_id') and 'targets' in result
                    and 'observe' in self.registry.descriptor('broker',broker)['capabilities']):
                self.monitor.cycle(broker,result,self.config()[0]['strategy'])
            if action in {'execute','recover','reconcile'}:
                self.monitor.next_observation = 0
            self.monitor.events.transition('reporting:'+broker, True, '账户表现记录已恢复。', 'positions', notify=notify)
        except Exception:
            self.monitor.error = '账户表现记录未通过校验，已有交易结果与历史记录已保留。'
            # One durable health flag per account: a persistent validation failure
            # produces one event (and one notification), not one per observation.
            self.monitor.events.transition('reporting:'+broker, False, self.monitor.error, 'positions', notify=notify)

    def monitoring_poll(self):
        with self.lock:
            if self.closing:
                return
            self.monitor.automation_events(list(self.automation.data['events']))
            self.monitor.pump()
            if not self.monitor.settings.config['enabled']:
                self.monitor.error = None
                return
            if self.active or time.time() < self.monitor.next_observation:
                return
            self.monitor.next_observation = time.time()+self.monitor.settings.config['interval_seconds']
            try:
                if 'observe' not in self.registry.descriptor('broker',self.broker_id)['capabilities']:
                    self.monitor.unconfigured = '当前账户模块没有提供账户表现接口，定时监控不覆盖它。'
                    return
                try:
                    self.monitor.scope(self.broker_id)
                except ControlError as exc:
                    # Not bound yet is a setup step, not a monitoring failure: no
                    # error, no health transition, no notification.
                    self.monitor.unconfigured = f'账户尚未完成绑定，监控暂未开始：{exc}'
                    return
                self.monitor.unconfigured = None
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
                for action in ('resolve-remainder', 'close-terminal', 'close-untransmitted'):
                    actions[action] = {'enabled': False, 'blockers': [], 'message': '请先关闭自动运行，再处理未提交余单。', 'next_action': None}
            managed = self.managed_account(self.broker_id)
            if managed:
                for action in ('execute', 'resolve-remainder', 'close-terminal', 'close-untransmitted'):
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
                'backfill': self.backfill.public(),
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

    def start(self, action, credentials=None, *, as_of=None):
        with self.lock:
            self.idle()
            if action not in WORKBENCH_ACTIONS:
                raise PortalError('未知操作。')
            if action == 'backfill' and not as_of:
                raise PortalError('历史回填请在「回测 → 历史回填」中按日期区间启动。')
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
                   'message': '正在处理…', 'events': [], 'cancel_available': False,
                   # Routine observations run without the user asking; the UI keeps
                   # editors and forms usable while they run.
                   'background': action == 'observe', **({'as_of': as_of} if action == 'backfill' else {})}
            self.active = job
            self.jobs.insert(0, job)
            self.jobs = self.jobs[:40]
            self._save_job(job)

            def progress(message):
                with self.lock:
                    job['message'] = self._public(str(message), secret_values)[:800]
                    job['events'] = (job['events'] + [{'at': now(), 'message': job['message']}])[-60:]
                    job['cancel_available'] = action in {'refresh', 'backfill'} and 'cancel' in source_spec['capabilities']
                    self._save_job(job)

            def work():
                try:
                    result = self.worker(self.root, action, snapshot_path, token, progress,
                                         source_id=source, broker_id=broker_id, expected_context=context, credentials=credentials,
                                         **({'as_of': as_of} if action == 'backfill' else {}))
                except Exception as exc:
                    # Vendor exceptions can contain credentials. Never log their raw text.
                    result = dict(failure_details(exc, action, fallback='操作未取得有效结果，请检查本地配置；程序不会自动重试。'),
                                  status='paused' if action == 'execute' else 'blocked')
                finally:
                    if credentials is not None:
                        credentials.clear()
                with self.lock:
                    try:
                        self._finish_job(job, action, result, broker_id, source, context, binding,
                                         snapshot_path, financial_scope, secret_values)
                    except Exception as exc:
                        self._finalization_failed(job, exc)
                    finally:
                        if self.active is job:
                            self.active = None

            self.thread = threading.Thread(target=work, name='portal-worker', daemon=False)
            self.thread.start()
            return {'job_id': job['id']}

    def _finish_job(self, job, action, result, broker_id, source, context, binding, snapshot_path,
                    financial_scope, secret_values):
        """Record a worker result under the service lock (called once per job)."""
        self._record_observation(action, broker_id, result, financial_scope)
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
                        self.account = {'status': 'connected', 'message': '准备时已核对绑定的账户。',
                                        'details': safe_result['account_details'],
                                        'checked_at': now()}
        if action == 'prepare' and result.get('status') == 'complete' and result.get('no_orders') is True:
            # "Nothing to trade" is a result worth keeping on screen, exactly
            # as the one-click check publishes it; it can never be submitted.
            try:
                still_current = context_for(self.registry, action, broker_id, source) == context
            except (ValueError, OSError, KeyError):
                still_current = False
            if still_current:
                self.preview = dict(safe_result, execution_ready=False, created_at=now(), source=source, broker=broker_id)
                self.preview_context = context
                self.preview_private = None
        if action == 'execute':
            self.account = None  # Do not display pre-trade holdings as current.
        if action in {'reconcile', 'resolve-remainder', 'close-terminal', 'close-untransmitted'}:
            self.automation.reconciled(broker_id, result)
        if action == 'backfill':
            self.backfill.record(job.get('as_of'), safe_result, now())
            self.scheduler_wake.set()
        self._save_job(job)

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

    def automation_migrate(self, value=None):
        """Turn the single-strategy plan into a stopped run instance, then close the plan.

        The instance gets the plan's account, data source, schedule and the
        current manual strategy parameters with a 100% budget, so its sizing
        matches what the plan did. It is created stopped: starting is a
        separate, confirmed step. The plan's history stays readable.
        """
        with self.lock:
            self.idle()
            if value:
                raise PortalError('迁移不接受额外参数。')
            plan = self.automation
            if plan.corrupt:
                raise PortalError('自动计划状态文件损坏，无法迁移；请先检查文件。')
            binding = plan.data.get('binding') or {}
            if not binding.get('broker_id') or not binding.get('source_id'):
                raise PortalError('没有绑定账户的单策略计划可迁移；直接用「新建运行」即可。')
            if any(c['status'] in {'pending', 'blocked', 'preparing'} for c in plan.data['cycles'].values()):
                raise PortalError('单策略计划仍有未核对的批次；先核对订单或结束余单，再迁移。')
            try:
                unchanged = self.registry.context(binding['broker_id'], binding['source_id']) == binding.get('context')
            except (ControlError, ValueError, OSError, KeyError, TypeError):
                unchanged = False
            if not unchanged:
                raise PortalError('模块、策略参数或代码与计划绑定时不同；请先核对「参数」页的配置，或直接用「新建运行」。')
            cfg, _ = self.config()
            name = f"{binding.get('strategy_name') or cfg['strategy']['type']} · 迁移自单策略计划"[:80]
            payload = {'name': name, 'strategy': deepcopy(cfg['strategy']), 'broker_id': binding['broker_id'],
                       'source_id': binding['source_id'], 'budget': '1', 'schedule': deepcopy(plan.data['config'])}
            created = self.strategy_instances.create(payload)
            if plan.data['enabled'] or plan.data['status'] != 'disabled':
                plan.disable()
            plan.data['migrated_to'] = created['id']
            plan.event(f'已迁移为运行实例「{name}」；计划已关闭，历史记录保留。')
            plan.persist()
            self.clear_preview()
            self.scheduler_wake.set()
            return {'id': created['id'], 'name': name,
                    'message': '已创建运行实例（资金比例 100%，处于停止状态）；旧计划已关闭，历史保留。请在运行详情里确认启动。'}

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
                    self.backfill_poll()
                except Exception as exc:
                    with self.lock:
                        log_exception(log, 'backfill poll', exc, incident_id())
                        self.backfill.pause('回填调度遇到异常并已暂停，请查看运行日志。', now())
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
                    try:
                        if not self.automation.corrupt:
                            self.automation.pause(result['message'])
                    except Exception as failure:
                        log_exception(log, 'automation pause after failure', failure, incident_id())
                finally:
                    with self.lock:
                        try:
                            self._finish_automation(job, result, binding)
                        except Exception as exc:
                            self._finalization_failed(job, exc)
                        finally:
                            if self.active is job:
                                self.active = None
            self.thread = threading.Thread(target=work, name='automatic-paper-worker', daemon=False)
            self.thread.start()

    def _finish_automation(self, job, result, binding):
        """Record one automation tick under the service lock."""
        if result is None:
            return
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

    def _backfill_context(self, cfg):
        """The source, strategy and data request a backfill belongs to; any change pauses it."""
        return {'source': self.source, 'strategy_type': cfg['strategy']['type'],
                'request_id': self.registry.data_requirements(cfg).fingerprint}

    def backfill_plan(self, value):
        """Dates a historical collection would cover, which ones already have a snapshot, and the request cost."""
        if not isinstance(value, dict):
            raise PortalError('回填参数无效。')
        try:
            start, end = date.fromisoformat(str(value.get('start'))), date.fromisoformat(str(value.get('end')))
        except (TypeError, ValueError):
            raise PortalError('请填写回填的开始和结束日期。') from None
        cadence = value.get('cadence', 'monthly')
        if start > end or cadence not in CADENCES:
            raise PortalError('回填区间或周期无效。')
        with self.lock:
            cfg, _ = self.config()
            check_expected_strategy(value.get('expected_strategy_type', _UNSPECIFIED_STRATEGY), cfg['strategy']['type'])
            allowed = self.state()['actions']['backfill']
            blockers = [b for b in allowed['blockers'] if b['code'] != 'busy']  # planning is allowed while a job runs
            if blockers:
                raise PortalError(blockers[0]['message'])
            today = datetime.now(ZoneInfo('America/New_York')).date()
            dates = schedule_dates(start, end, cadence, today)
            if not dates:
                raise PortalError('所选区间内没有可回填的日期；日期必须早于今天。')
            if len(dates) > MAX_DATES:
                raise PortalError(f'一次最多回填 {MAX_DATES} 个日期，请缩小区间。')
            context = self._backfill_context(cfg)
            source, items = self.replay_series()
            existing = [date.fromisoformat(item['date']) for item in items]
            plan = []
            for day in dates:
                hit = covered(day, existing)
                plan.append({'as_of': str(day), 'status': 'skipped' if hit else 'pending', 'job_id': None,
                             'message': f'已有 {hit} 的快照，跳过。' if hit else ''})
            pending = sum(item['status'] == 'pending' for item in plan)
            requirements = self.registry.data_requirements(cfg)
            bars = max(requirements.price_bars, requirements.volume_bars)
            income = 'operating_income_ttm' in requirements.fields
            details = max((item.get('details_checked') or 0 for item in items), default=0) or 120
            adapter = self.registry.control('data', source, config=cfg)
            profile = adapter.backfill_profile() if hasattr(adapter, 'backfill_profile') else {}
            per_minute = profile.get('requests_per_minute') or 5
            requests = estimate_requests(pending, history_bars=bars, details=details, income_required=income,
                                         listing_pages=profile.get('listing_pages') or 6, cadence=cadence)
            estimate = {'requests': requests, 'minutes': -(-requests // per_minute), 'requests_per_minute': per_minute,
                        'details_per_date': details, 'history_bars': bars, 'income_required': income}
            return dict(context, cadence=cadence, start=str(start), end=str(end), dates=plan,
                        pending_count=pending, skipped_count=len(plan) - pending, estimate=estimate)

    def backfill_start(self, value):
        with self.lock:
            if self.backfill.active:
                raise PortalError('已有回填在进行；请先取消，或等待它完成。')
            plan = self.backfill_plan(value)
            if not plan['pending_count']:
                raise PortalError('所选区间的日期都已有快照，无需回填。')
            result = self.backfill.create(source=plan['source'], strategy_type=plan['strategy_type'],
                                          request_id=plan['request_id'], cadence=plan['cadence'], start=plan['start'],
                                          end=plan['end'], dates=plan['dates'], estimate=plan['estimate'], now=now())
            self.scheduler_wake.set()
            return dict(result, message=f"历史回填已排队：{plan['pending_count']} 个日期，预计 {plan['estimate']['requests']} 次请求。")

    def backfill_control(self, command):
        with self.lock:
            queue = self.backfill
            if not queue.data:
                raise PortalError('没有回填任务。')
            stamp = now()
            if command == 'pause':
                if queue.data['status'] != 'running':
                    raise PortalError('回填不在运行中。')
                queue.pause('已暂停；正在采集的日期完成后不再继续。', stamp)
            elif command == 'resume':
                if queue.data['status'] != 'paused':
                    raise PortalError('回填不在暂停状态。')
                queue.resume(stamp)
                self.scheduler_wake.set()
            elif command == 'cancel':
                if not queue.active:
                    raise PortalError('回填已经结束。')
                queue.cancel(stamp)
                if self.active and self.active.get('action') == 'backfill' and self.active.get('cancel_available'):
                    self.cancel_download()
            else:
                raise PortalError('未知的回填操作。')
            return dict(queue.public(), message=queue.data['message'])

    def backfill_poll(self):
        """Start the next queued date when the workbench is idle; never while another job runs."""
        with self.lock:
            queue = self.backfill
            if self.closing or self.active or not queue.data or queue.data['status'] != 'running':
                return
            stamp = now()
            item = queue.next_pending()
            if item is None:
                queue.finish(stamp)
                return
            try:
                cfg, _ = self.config()
                if self._backfill_context(cfg) != {k: queue.data[k] for k in ('source', 'strategy_type', 'request_id')}:
                    queue.pause('数据源、策略或策略参数与回填开始时不同，回填已暂停；切回后可继续。', stamp)
                    return
                started = self.start('backfill', as_of=item['as_of'])
            except PortalError as exc:
                queue.pause(f'回填无法开始：{exc}', stamp)
                return
            queue.begin(item, started['job_id'], stamp)

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
                        evidence = read_json(result / 'raw-result.json')
                        if parse_any(evidence) != record['result']:
                            raise ValueError('Changed summary')
                        item = {'id': path.name + '/' + result.name,
                                'provider': record['result'].get('provider', 'quantconnect'),
                                'metrics': record['result']['metrics'],
                                'completion': record['result']['completion'],
                                'code_evidence': record['code_evidence'],
                                'warnings': record['warnings']}
                        if item['provider'] == 'local_replay':
                            # The curve and the rebalance log are part of the verified evidence file.
                            item.update(nav=evidence.get('nav', []), rebalances=evidence.get('rebalances', []),
                                        replay_warnings=evidence.get('warnings', []), snapshot_count=evidence.get('snapshot_count'))
                        entry['results'].append(item)
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
                out = import_result(experiment, path, parse_any)
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
            return compare_results(folders, parse_any, self.folder / 'comparisons' / uuid4().hex)

    def replay_series(self):
        """Dated snapshots saved for the manual data source, oldest first.

        Every collection and upload leaves a file; one file per price date is
        kept (the newest). Replays run on this series only: it is the data the
        workbench actually had, not a reconstructed history.
        """
        with self.lock:
            cfg, _ = self.config()
            source = self.source
            adapter = self.registry.control('data', source, config=cfg)
            folders = [Path(adapter.directory), self.folder / 'snapshots']
        found = {}
        for folder in folders:
            if not folder.is_dir():
                continue
            for path in [*sorted(folder.glob('*/*.json')), *sorted(folder.glob('*.json'))]:
                if path.name in {'progress.json', 'failure.json', 'latest.json'} or path.stat().st_size > MAX_FILE:
                    continue
                try:
                    raw = read_json(path)
                    meta = adapter.metadata(raw)
                    day = meta.get('price_as_of')
                    if not day and isinstance(raw.get('stocks'), list):
                        # Uploaded files may carry the date per stock only.
                        days = [s.get('price_as_of') for s in raw['stocks'] if isinstance(s, dict) and s.get('price_as_of')]
                        day = max(days) if days else None
                    day = date.fromisoformat(str(day)).isoformat()
                except (ControlError, ValueError, TypeError, OSError, KeyError):
                    continue
                entry = found.get(day)
                if entry is None or path.stat().st_mtime > entry['mtime']:
                    found[day] = {'date': day, 'path': path, 'mtime': path.stat().st_mtime,
                                  'candidate_count': meta.get('candidate_count'), 'research_only': meta.get('research_only', False),
                                  'details_checked': (meta.get('coverage') or {}).get('details_checked')}
        return source, [found[day] for day in sorted(found)]

    def replay_backtest(self, value):
        """Replay the manual configuration over the saved snapshot series and archive the result."""
        if not isinstance(value, dict):
            raise PortalError('回放参数无效。')
        start, end, slip = value.get('start'), value.get('end'), value.get('slippage_bps', 10)
        cadence = value.get('cadence', 'monthly')
        try:
            start, end = date.fromisoformat(str(start)).isoformat(), date.fromisoformat(str(end)).isoformat()
        except (TypeError, ValueError):
            raise PortalError('请填写回放的开始和结束日期。') from None
        if start > end or cadence not in {'daily', 'weekly', 'monthly'}:
            raise PortalError('回放区间或调仓周期无效。')
        if isinstance(slip, bool) or not isinstance(slip, (int, float)) or not 0 <= slip <= 9999:
            raise PortalError('滑点应为 0–9999 bps。')
        with self.lock:
            self.idle()
            cfg, _ = self.config()
            check_expected_strategy(value.get('expected_strategy_type', _UNSPECIFIED_STRATEGY), cfg['strategy']['type'])
        source, items = self.replay_series()
        chosen = [item for item in items if start <= item['date'] <= end]
        if len(chosen) < 2:
            raise PortalError(f'所选区间内只有 {len(chosen)} 份已保存的快照；本地回放至少需要 2 份不同日期的快照。')
        strategy = build_portfolio_strategy(cfg['strategy'])
        series = []
        for item in chosen:
            try:
                stocks = load_snapshot(item['path'])[0]
            except (ValueError, KeyError, TypeError):
                adapter = self.registry.control('data', source, config=cfg)
                stocks = tuple(adapter.load(read_json(item['path'])).stocks)
            series.append((date.fromisoformat(item['date']), stocks))
        try:
            result = replay(series, strategy, max_gross=cfg['limits']['max_gross'], max_weight=cfg['limits']['max_weight'],
                            initial_cash=cfg['initial_cash'], fee_per_order=cfg['fees']['per_order'],
                            fee_bps=cfg['fees']['bps'], slippage_bps=slip, cadence=cadence)
        except ValueError as exc:
            raise PortalError(f'本地回放未能完成：{exc}') from None
        result.update(source=source, strategy=cfg['strategy'],
                      snapshots=[{'date': item['date'], 'file': str(item['path'].relative_to(self.root)),
                                  'candidate_count': item['candidate_count']} for item in chosen])
        exported = self.export(result['period']['start'], result['period']['end'], slip,
                               note=f'本地回放 · {len(chosen)} 份快照 · {cadence}', expected_strategy_type=cfg['strategy']['type'])
        experiment = self._experiment(exported['id'])
        path = self.folder / ('replay-' + uuid4().hex + '.json')
        atomic_json(path, result)
        try:
            out = import_result(experiment, path, parse_any)
        finally:
            path.unlink(missing_ok=True)
        return {'id': exported['id'] + '/' + out.name, 'experiment': exported['id'], 'metrics': result['metrics'],
                'snapshot_count': len(chosen), 'rebalance_count': result['rebalance_count'], 'warnings': result['warnings'],
                'message': f'本地回放完成：{len(chosen)} 份快照，{result["rebalance_count"]} 次调仓。'}
