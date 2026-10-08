"""Persistent strategy instances, account-level scheduling and calculation limits."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.automation import code_revision, save
from app.control_contracts import ControlError, project_path
from framework.decisions import digest
from framework.scheduling import DEFAULT_SCHEDULE, initial_window, next_window, period_window, period_key, schedule_config, normalize_sessions, session_dates, schedule_defaults
from framework.trading import amount

ID = re.compile(r'st-[0-9a-f]{16}')
MAX_INSTANCES = 16


def utcnow():
    return datetime.now(timezone.utc)


def identity(item):
    return {k: item[k] for k in ('id', 'strategy', 'budget', 'schedule', 'broker_id', 'source_id', 'generation')}


def preview_revision(root, item, positions, *, code=None):
    return digest(dict(instance=identity(item), positions=positions,
                       code=code_revision(Path(root)) if code is None else code))


def managed_guard(root, cycle, *, clock=utcnow):
    """Every new net order requires unchanged instances and an unrevoked window."""
    from app.module_registry import ModuleRegistry
    try:
        state = json.loads((Path(root) / 'runs/strategy_manager/state.json').read_text(encoding='utf-8'))
        records = {item['id']: item for item in state['instances']}
        if state.get('corrupt') or code_revision(Path(root)) != cycle['code_revision']:
            raise ValueError()
        if not datetime.fromisoformat(cycle['window_start']) <= clock() < datetime.fromisoformat(cycle['window_end']):
            raise ValueError()
        registry = ModuleRegistry(root)
        for original in cycle['instances']:
            current = records[original['id']]
            if identity(current) != identity(original) or (original['id'] in cycle['due_ids'] and not current['enabled']):
                raise ValueError()
            if instance_binding(registry, current) != cycle['bindings'][original['id']]:
                raise ValueError()
        owned = [item['id'] for item in state['instances'] if item['broker_id'] == cycle['broker_id']
                 and (item['enabled'] or state['books'].get(cycle['broker_id'], {}).get('owners', {}).get(item['id']))]
        if set(owned) != {item['id'] for item in cycle['instances']}:
            raise ValueError()
        latest = json.loads(project_path(root, cycle['path'], under='runs/strategy_manager/cycles').read_text(encoding='utf-8'))
        if latest.get('cancelled') or latest.get('id') != cycle['id']:
            raise ValueError()
    except Exception:
        raise ControlError('策略已停止、配置已变化或执行窗口结束；新委托已停止。') from None


def instance_binding(registry, item):
    return digest({'instance': identity(item), 'source': registry.source_context(item['source_id']),
                   'broker': registry.connection_context(item['broker_id'])})


def require_unmanaged(root, registry, broker_id):
    """Keep legacy/manual submissions out of an instance-owned account ledger."""
    manager = StrategyInstances(root, registry, recover_pending=False)
    if manager.corrupt:
        raise ControlError('策略持仓归属状态无法核对，请先检查策略运行记录。')
    if manager.owns_account(broker_id):
        raise ControlError('此账户由策略实例管理，请在「策略运行」中操作。')
    location = getattr(registry.control('broker', broker_id), 'state_dir', None)
    if location is not None:
        for item in manager.data['instances']:
            if manager.owns_account(item['broker_id']) and getattr(registry.control('broker', item['broker_id']), 'state_dir', None) == location:
                raise ControlError('此账户由另一配置的策略实例管理，请使用同一账户配置。')


class StrategyInstances:
    def __init__(self, root, registry, *, clock=utcnow, lock=None, recover_pending=False):
        self.root, self.registry, self.clock = Path(root).resolve(), registry, clock
        self.lock = lock or threading.RLock()
        self.folder = self.root / 'runs/strategy_manager'
        self.path = self.folder / 'state.json'
        self.data = dict(schema=1, instances=[], books={}, pending={}, calendar={},
                         limits={'selection_timeout_seconds': 60, 'log_retention_runs': 100})
        self.corrupt = False
        self.active_broker_id = None
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if raw['schema'] != 1 or not isinstance(raw['instances'], list) or len(raw['instances']) > MAX_INSTANCES:
                    raise ValueError()
                ids = set()
                for item in raw['instances']:
                    if not ID.fullmatch(item['id']) or item['id'] in ids or type(item['enabled']) is not bool:
                        raise ValueError()
                    ids.add(item['id'])
                    amount(item['budget'], True)
                    schedule_config(item['schedule'])
                for key in ('books', 'pending', 'calendar', 'limits'):
                    if not isinstance(raw[key], dict):
                        raise ValueError()
                self.data.update(raw)
                # A restarted account batch is never silently replayed.
                for broker_id in self.data['pending'] if recover_pending else ():
                    for item in self.data['instances']:
                        if item['broker_id'] == broker_id:
                            item.update(enabled=False, status='paused', next_run=None, next_check=None,
                                        generation=uuid4().hex, force_run=False, one_shot=False,
                                        message='上次账户批次被中断；请先核对订单与持仓归属。')
                if recover_pending and self.data['pending']:
                    self.persist()  # Revoke any old batch guard on disk as well.
            except Exception:
                self.corrupt = True

    def persist(self):
        if self.corrupt:
            raise ControlError('策略运行状态无法核对，已停用；原文件保留供检查。')
        save(self.path, self.data)

    def get(self, ident):
        if not isinstance(ident, str) or not ID.fullmatch(ident):
            raise ControlError('策略实例编号无效。')
        item = next((x for x in self.data['instances'] if x['id'] == ident), None)
        if item is None:
            raise ControlError('策略实例已不存在。')
        return item

    def owns_account(self, broker_id):
        book = self.data['books'].get(broker_id, {}).get('owners', {})
        return any(item['broker_id'] == broker_id and (item['enabled'] or book.get(item['id']))
                   for item in self.data['instances']) or broker_id in self.data['pending']

    def reserved_budget(self, broker_id, *, exclude=None):
        book = self.data['books'].get(broker_id, {}).get('owners', {})
        return sum((amount(x['budget']) for x in self.data['instances']
                    if x['broker_id'] == broker_id and x['id'] != exclude
                    and (x['enabled'] or book.get(x['id']))), amount(0))

    def require_budget(self, broker_id, ident, budget):
        if self.reserved_budget(broker_id, exclude=ident) + amount(budget) > 1:
            raise ControlError('运行及保留持仓的实例资金比例合计超过 100%，请调整分配。')

    def validate(self, value, ident=None):
        allowed = {'name', 'strategy', 'broker_id', 'source_id', 'budget', 'schedule'}
        if not isinstance(value, dict) or set(value) != allowed:
            raise ControlError('请填写名称、策略、资金比例、账户、数据源与计划。')
        name = value['name']
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ControlError('实例名称应为 1 至 80 个字符。')
        try:
            budget = amount(value['budget'], True)
            if budget > 1:
                raise ValueError()
            broker = self.registry.descriptor('broker', value['broker_id'])
            schedule = schedule_config(value['schedule'], defaults=schedule_defaults(broker.get('execution_context')))
        except (ValueError, TypeError, KeyError):
            raise ControlError('资金比例或运行计划无效。') from None
        broker = self.registry.descriptor('broker', value['broker_id'])
        if not broker['enabled'] or broker['mode'] not in {'broker_paper', 'live'}:
            raise ControlError('策略实例需要提供执行能力的联网账户模块。')
        if not {'prepare', 'execute', 'reconcile', 'calendar'} <= set(broker['capabilities']):
            raise ControlError('此账户尚未提供策略运行所需接口。')
        cfg = self.registry.configuration(value['broker_id'])
        cfg['strategy'] = deepcopy(value['strategy'])
        self.registry.validate_strategy(cfg)
        self.registry.compatible(cfg, value['source_id'])
        source = self.registry.descriptor('data', value['source_id'])
        if ('collect' not in source['capabilities'] or not {'execution_signal', 'paper_signal'} & set(source['capabilities'])):
            raise ControlError('请选择能够采集数据并准备交易信号的数据源。')
        return dict(value, name=name.strip(), budget=str(budget), schedule=schedule)

    def create(self, value):
        if len(self.data['instances']) >= MAX_INSTANCES:
            raise ControlError(f'最多管理 {MAX_INSTANCES} 份策略实例。')
        clean = self.validate(value)
        item = dict(clean, id='st-' + uuid4().hex[:16], enabled=False, status='stopped',
                    message='尚未启动。', generation=uuid4().hex, created_at=self.clock().isoformat(),
                    started_at=None, stopped_at=None, last_run=None, next_run=None, initialized=False,
                    last_period=None, next_check=None, events=[], binding=None)
        self.data['instances'].append(item)
        self.persist()
        return {'message': '策略实例已创建。', 'id': item['id']}

    def update(self, ident, value):
        item = self.get(ident)
        if item['enabled'] or item['broker_id'] in self.data['pending']:
            raise ControlError('先停止实例并核对未完成批次，再修改设置。')
        positions = self.data['books'].get(item['broker_id'], {}).get('owners', {}).get(ident)
        if positions and (value.get('broker_id') != item['broker_id']):
            raise ControlError('实例仍有持仓，无法转移到另一个账户。')
        clean = self.validate(value, ident)
        if positions:
            self.require_budget(clean['broker_id'], ident, clean['budget'])
        item.update(clean, generation=uuid4().hex, initialized=False, last_period=None,
                    next_run=None, next_check=None, binding=None, message='设置已保存，重新启动后检查。')
        self.persist()
        return {'message': item['message']}

    def start(self, ident):
        item = self.get(ident)
        self.validate({k: item[k] for k in ('name', 'strategy', 'broker_id', 'source_id', 'budget', 'schedule')})
        broker = self.registry.descriptor('broker', item['broker_id'])
        if not broker.get('execution_enabled', broker['mode'] != 'live'):
            raise ControlError(broker.get('execution_reason') or '此账户尚未完成独立执行授权；策略配置已保留。')
        if item['enabled']:
            raise ControlError('此实例已开启。')
        if item['broker_id'] in self.data['pending']:
            raise ControlError('此账户仍有未核对批次，请先核对订单与归属。')
        self.require_budget(item['broker_id'], ident, item['budget'])
        item.update(enabled=True, status='checking', started_at=self.clock().isoformat(), stopped_at=None,
                    generation=uuid4().hex, next_check=None, next_run=None, check_pending=True,
                    initial_window=None, force_run=False, one_shot=False,
                    message='正在检查账户、数据源和运行计划。')
        item['binding'] = instance_binding(self.registry, item)
        item['code_revision'] = code_revision(self.root)
        self.event(item, item['message'])
        self.persist()
        return {'message': '实例已开启，后台立即检查。'}

    def stop(self, ident):
        item = self.get(ident)
        item.update(enabled=False, status='stopped', generation=uuid4().hex,
                    stopped_at=self.clock().isoformat(), next_run=None, next_check=None,
                    force_run=False, one_shot=False, check_pending=False, initial_window=None,
                    message='已停止后续运行；保留持仓，已提交订单仍需核对。')
        self.event(item, item['message'])
        self.persist()
        return {'message': item['message']}

    def run_once(self, ident):
        item = self.get(ident)
        if item.get('force_run'):
            raise ControlError('此实例已请求运行一轮，请等待结果。')
        was_enabled = item['enabled']
        if not was_enabled:
            self.start(ident)
        item.update(force_run=True, one_shot=not was_enabled, initial_window=None, next_check=None)
        self.persist()
        return {'message': '已请求运行一轮；先检查，休市时等待可用交易窗口。'}

    def remove(self, ident):
        item = self.get(ident)
        if item['enabled'] or item['broker_id'] in self.data['pending'] or self.data['books'].get(item['broker_id'], {}).get('owners', {}).get(ident):
            raise ControlError('停止实例并清空其已确认持仓后才能删除；运行记录会保留。')
        self.data['instances'] = [x for x in self.data['instances'] if x['id'] != ident]
        self.persist()
        return {'message': '实例已删除，历史记录已保留。'}

    def event(self, item, message, severity='info'):
        item['events'] = [*item['events'], dict(at=self.clock().isoformat(), message=str(message)[:800], severity=severity)][-60:]

    def pause_account(self, broker_id, message):
        for item in self.data['instances']:
            if item['broker_id'] == broker_id and item['enabled']:
                item.update(enabled=False, status='paused', next_run=None, next_check=None, message=message)
                self.event(item, message, 'error')
        self.persist()

    def due(self):
        return not self.corrupt and any(x['enabled'] and (not x.get('next_check')
            or self.clock() >= datetime.fromisoformat(x['next_check'])) for x in self.data['instances'])

    def configure_limits(self, value):
        if any(x['enabled'] for x in self.data['instances']) or self.data['pending']:
            raise ControlError('停止所有实例并核对批次后再修改运行限制。')
        if not isinstance(value, dict) or set(value) != {'selection_timeout_seconds', 'log_retention_runs'}:
            raise ControlError('运行限制格式无效。')
        if (type(value['selection_timeout_seconds']) is not int or not 10 <= value['selection_timeout_seconds'] <= 120
                or type(value['log_retention_runs']) is not int or not 20 <= value['log_retention_runs'] <= 500):
            raise ControlError('计算时限应为 10–120 秒，日志保留应为 20–500 轮。')
        self.data['limits'] = dict(value)
        self.persist()
        return {'message': '策略计算限制已保存。'}

    def public(self):
        items = []
        code = code_revision(self.root) if self.data['instances'] else None
        for record in self.data['instances']:
            value = {k: record.get(k) for k in ('id', 'name', 'strategy', 'budget', 'broker_id', 'source_id',
                'schedule', 'enabled', 'status', 'message', 'started_at', 'stopped_at', 'next_run', 'last_run')}
            value['events'] = list(reversed(record['events'][-30:]))
            value['positions'] = [{'ticker': s, 'quantity': q} for s, q in
                self.data['books'].get(record['broker_id'], {}).get('owners', {}).get(record['id'], {}).items()]
            value['pending'] = record['broker_id'] in self.data['pending']
            positions = self.data['books'].get(record['broker_id'], {}).get('owners', {}).get(record['id'], {})
            value['revision'] = preview_revision(self.root, record, positions, code=code)
            items.append(value)
        accounts = []
        for broker_id in sorted({x['broker_id'] for x in items}):
            group = [x for x in items if x['broker_id'] == broker_id]
            reserved = self.reserved_budget(broker_id)
            accounts.append(dict(broker_id=broker_id, reserved_budget=str(reserved),
                available_budget=str(max(amount(0), 1 - reserved)), instance_count=len(group),
                running_count=sum(x['enabled'] for x in group),
                stopped_holding_count=sum(not x['enabled'] and bool(x['positions']) for x in group),
                pending=broker_id in self.data['pending']))
        return dict(instances=items, limits=self.data['limits'], corrupt=self.corrupt,
                    running_count=sum(x['enabled'] for x in items), max_instances=MAX_INSTANCES,
                    accounts=accounts,
                    unmanaged={b: book.get('unmanaged', {}) for b, book in self.data['books'].items()})

    def tick(self, worker, progress=lambda message: None):
        """One account batch at a time. Portal holds the global operation lock."""
        # State writes share the Portal lock, but network waits release it so Stop
        # remains responsive and revokes the order guard immediately.
        def unlocked_worker(*args, **kwargs):
            self.lock.release()
            try:
                return worker(*args, **kwargs)
            finally:
                self.lock.acquire()
        with self.lock:
            return self._tick(unlocked_worker, progress)

    def _tick(self, worker, progress):
        self.active_broker_id = None
        now = self.clock()
        enabled = [x for x in self.data['instances'] if x['enabled']]
        for item in enabled:
            try:
                broker = self.registry.descriptor('broker', item['broker_id'])
                valid = (broker.get('execution_enabled', broker['mode'] != 'live')
                         and item['binding'] == instance_binding(self.registry, item) and item['code_revision'] == code_revision(self.root))
            except Exception:
                valid = False
            if not valid:
                self.pause_account(item['broker_id'], '代码、账户或数据配置已变化，请核对后重新启动实例。')
                return {'status': 'paused', 'message': '策略配置变化，已暂停相关账户。'}
        if not enabled:
            return {'status': 'complete', 'message': '没有开启的策略实例。'}
        ready = [x for x in enabled if not x.get('next_check') or now >= datetime.fromisoformat(x['next_check'])]
        if not ready:
            return {'status': 'complete', 'message': '等待下一次策略检查。'}
        broker_id = ready[0]['broker_id']
        self.active_broker_id = broker_id
        group = [x for x in enabled if x['broker_id'] == broker_id]
        calendar = self.data['calendar'].get(broker_id)
        calendar_zone = ZoneInfo(group[0]['schedule']['timezone'])
        if not calendar or calendar['day'] != str(now.astimezone(calendar_zone).date()):
            value = worker(self.root, 'calendar', broker_id=broker_id, source_id=group[0]['source_id'])
            if value.get('status') != 'ready':
                self.pause_account(broker_id, value.get('message', '交易日历无法核对。'))
                return {'status': 'paused', 'message': '策略账户交易日历无法核对。'}
            calendar = {'day': str(now.astimezone(calendar_zone).date()), 'sessions': value['sessions']}
            self.data['calendar'][broker_id] = calendar
        from datetime import date
        sessions = normalize_sessions(calendar['sessions'])
        due, starts, ends = [], [], []
        shared_prepared = {}
        for item in group:
            if not item['enabled']:
                continue
            item['next_check'] = (now + timedelta(seconds=item['schedule']['poll_seconds'])).isoformat()
            if item.get('check_pending'):
                check = worker(self.root, 'strategy-check', instance=deepcopy(item), progress=progress)
                if not item['enabled']:
                    continue
                if check.get('status') != 'complete':
                    item.update(enabled=False, status='paused', next_run=None, message=check.get('message', '前置检查未通过。'))
                    self.event(item, item['message'], 'error')
                    continue
                reference = check.get('account_ref')
                duplicate = any(x['broker_id'] != broker_id and x['enabled'] and x.get('account_ref') == reference
                                for x in self.data['instances']) or any(b != broker_id and book.get('account_ref') == reference
                                and any(book.get('owners', {}).values()) for b, book in self.data['books'].items())
                if reference and duplicate:
                    item.update(enabled=False, status='paused', next_run=None,
                                message='多个账户配置指向同一账户，请统一使用一个账户配置。')
                    self.event(item, item['message'], 'error')
                    continue
                item['account_ref'] = reference
                item['check_pending'] = False
                self.event(item, '账户和数据接口已检查。')
            now = self.clock()
            local = now.astimezone(ZoneInfo(item['schedule']['timezone']))
            prepared = item.get('prepared')
            configured = self.registry.configuration(broker_id)
            configured['strategy'] = deepcopy(item['strategy'])
            data_key = (item['source_id'], self.registry.data_requirements(configured).fingerprint)
            if prepared and prepared.get('day') == str(local.date()):
                shared_prepared[data_key] = prepared
            if local.date() in session_dates(sessions) and local.strftime('%H:%M') >= item['schedule']['prepare_time'] and (
                    not prepared or prepared.get('day') != str(local.date())):
                progress('提前更新 ' + item['name'] + ' 的策略数据…')
                reusable = shared_prepared.get(data_key)
                refreshed = {'status': 'complete', 'snapshot_path': reusable['path']} if reusable else \
                    worker(self.root, 'strategy-refresh', instance=deepcopy(item), progress=progress)
                if not item['enabled']:
                    continue
                if refreshed.get('status') != 'complete':
                    item.update(enabled=False, status='paused', message=refreshed.get('message', '数据更新失败。'))
                    self.event(item, item['message'], 'error')
                    continue
                item['prepared'] = {'day': str(local.date()), 'path': refreshed['snapshot_path']}
                shared_prepared[data_key] = item['prepared']
                self.event(item, '已复用本轮相同需求的数据。' if reusable else '今日策略数据已更新。')
            if not item['enabled']:
                continue
            now = self.clock()
            item['next_check'] = (now + timedelta(seconds=item['schedule']['poll_seconds'])).isoformat()
            try:
                if not item['initialized'] or item.get('force_run'):
                    first_config = dict(item['schedule'], initial_entry='immediate') if item.get('force_run') else item['schedule']
                    start, end = initial_window(now, sessions, first_config)
                    if not item.get('initial_window'):
                        item['initial_window'] = [start.isoformat(), end.isoformat()]
                    start, end = (datetime.fromisoformat(t) for t in item['initial_window'])
                    if end <= now:
                        start, end = initial_window(now + timedelta(seconds=1), sessions, first_config)
                        item['initial_window'] = [start.isoformat(), end.isoformat()]
                    key = period_key(start.astimezone(ZoneInfo(item['schedule']['timezone'])).date(), item['schedule'])
                else:
                    start, end, key = period_window(now.astimezone(ZoneInfo(item['schedule']['timezone'])).date(), sessions, item['schedule'])
                    if key == item['last_period'] or end <= now:
                        next_at = next_window(max(now, end + timedelta(seconds=1)), sessions, item['schedule'])
                        item.update(status='waiting', next_run=next_at.isoformat() if next_at else None,
                                    message='等待下一次计划调仓。')
                        continue
                item.update(status='waiting', next_run=start.isoformat(), message='等待交易窗口。')
                if start <= now < end:
                    due.append(item)
                    starts.append(start)
                    ends.append(end)
                    item['cycle_period'] = key
            except (ValueError, KeyError):
                item.update(enabled=False, status='paused', message='交易日历未覆盖运行计划，请重新检查。')
                self.event(item, item['message'], 'error')
        self.persist()
        now = self.clock()
        due = [item for item in due if item['enabled']]
        if not due or now >= min(ends):
            return {'status': 'complete', 'message': '策略检查完成，等待各自交易计划。'}
        book = deepcopy(self.data['books'].get(broker_id, {'owners': {}, 'unmanaged': {}, 'account_ref': None}))
        participating = [x for x in self.data['instances'] if x['broker_id'] == broker_id
                         and (x['enabled'] or book['owners'].get(x['id']))]
        ident = 'cycle-' + uuid4().hex
        path = self.folder / 'cycles' / (ident + '.json')
        cycle = dict(id=ident, path=str(path), broker_id=broker_id, instances=deepcopy(participating),
                     due_ids=[x['id'] for x in due], bindings={x['id']: instance_binding(self.registry, x) for x in participating},
                     code_revision=code_revision(self.root), window_start=max(starts).isoformat(), window_end=min(ends).isoformat(),
                     book=book, limits=dict(self.data['limits']), phase='preparing', cancelled=False,
                     created_at=now.isoformat())
        save(path, cycle)
        self.data['pending'][broker_id] = str(path)
        for item in due:
            item.update(status='running', message='采集所需数据并合并账户目标。')
            self.event(item, item['message'])
        self.persist()
        result = worker(self.root, 'strategy-cycle', cycle_path=str(path), progress=progress)
        return self.finish_cycle(cycle, result)

    def finish_cycle(self, cycle, result):
        broker_id = cycle['broker_id']
        if result.get('status') == 'complete' and isinstance(result.get('book'), dict):
            self.data['books'][broker_id] = result['book']
            self.data['pending'].pop(broker_id, None)
            for ident in cycle['due_ids']:
                item = self.get(ident)
                item.update(initialized=True, last_period=next(x['cycle_period'] for x in cycle['instances'] if x['id'] == ident),
                            initial_window=None, last_run={'at': self.clock().isoformat(), 'cycle_id': cycle['id'],
                            'message': result['message']}, next_run=None, next_check=None)
                if item.get('one_shot'):
                    item.update(enabled=False, stopped_at=self.clock().isoformat())
                item.update(force_run=False, one_shot=False)
                item['status'] = 'waiting' if item['enabled'] else 'stopped'
                item['message'] = '本轮完成，等待下次计划。' if item['enabled'] else '已停止，保留已确认持仓。'
                self.event(item, result['message'])
        else:
            latest = json.loads(Path(cycle['path']).read_text(encoding='utf-8'))
            # Failure before submitting anything can safely discard the private preview.
            if not latest.get('execution_attempted'):
                self.data['pending'].pop(broker_id, None)
            self.pause_account(broker_id, result.get('message', '账户批次未完成，请核对订单。'))
        safe = {k: v for k, v in result.items() if k not in {'book', 'account_ref', 'snapshot_path', 'preview_token'}}
        save(self.folder / 'logs' / (cycle['id'] + '.json'), dict(cycle_id=cycle['id'],
             instance_ids=cycle['due_ids'], created_at=cycle['created_at'], **safe))
        logs = sorted((self.folder / 'logs').glob('cycle-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[self.data['limits']['log_retention_runs']:]:
            if not old.is_symlink() and old.resolve().is_relative_to((self.folder / 'logs').resolve()):
                old.unlink(missing_ok=True)
        self.persist()
        return safe

    def pending_cycle(self, ident):
        item = self.get(ident)
        path = self.data['pending'].get(item['broker_id'])
        if not path:
            return None
        return json.loads(project_path(self.root, path, under='runs/strategy_manager/cycles').read_text(encoding='utf-8'))

    def logs(self, ident):
        self.get(ident)
        result = []
        for path in sorted((self.folder / 'logs').glob('cycle-*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if ident in value.get('instance_ids', []):
                    result.append(value)
            except (ValueError, OSError):
                continue
            if len(result) >= self.data['limits']['log_retention_runs']:
                break
        return result
