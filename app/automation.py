"""Persistent, paper-only orchestration. Strategy and provider logic stay in plugins."""
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import threading
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.control_contracts import ControlError, project_path
from app.diagnostics import diagnostic_fields, failure_details
from framework.scheduling import DEFAULT_SCHEDULE, schedule_config, instant, period_window, next_window, period_key, initial_window


def utcnow():
    return datetime.now(timezone.utc)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def code_revision(root):
    digest = sha256()
    for folder in ('app', 'framework', 'strategies', 'brokers', 'data', 'platforms'):
        for path in sorted((root / folder).rglob('*.py')):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def submission_guard(root, permit, *, clock=utcnow):
    """Re-read revocation and the exact schedule immediately before each new intent."""
    from app.module_registry import ModuleRegistry
    path = Path(root) / 'runs/automation/state.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        binding = raw['binding']
        current = clock()
        if (not raw['enabled'] or raw['generation'] != permit['generation']
                or binding['context'] != permit['context'] or raw.get('status') == 'paused'
                or not datetime.fromisoformat(permit['window_start']) <= current < datetime.fromisoformat(permit['window_end'])
                or ModuleRegistry(root).context(binding['broker_id'], binding['source_id']) != binding['context']
                or code_revision(Path(root)) != binding['code_revision']):
            raise ValueError()
    except Exception:
        raise ControlError('自动运行已暂停、配置变化或交易窗口结束；不再提交新委托。') from None


class Automation:
    def __init__(self, root, registry, *, clock=utcnow):
        self.root, self.registry, self.clock = Path(root), registry, clock
        self.path = self.root / 'runs/automation/state.json'
        self.lock = threading.RLock()
        self.data = dict(schema=1, enabled=False, status='disabled', message='自动运行关闭；操作仍可手动执行。',
                         config=dict(DEFAULT_SCHEDULE), binding=None, generation=uuid4().hex,
                         cycles={}, daily={}, events=[], calendar=None, next_check=None, next_run=None, last_run=None,
                         startup=None)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if raw['schema'] != 1 or type(raw['enabled']) is not bool:
                    raise ValueError()
                raw['config'] = schedule_config(raw['config'])
                for name in ('cycles', 'daily'):
                    if not isinstance(raw[name], dict):
                        raise ValueError()
                if raw['enabled'] and not isinstance(raw['binding'], dict):
                    raise ValueError()
                self.data.update(raw)
            except Exception:
                self.data.update(status='paused', message='自动运行状态文件无法核对；已停止自动执行，请保留文件检查。')
                self.corrupt = True
                return
        self.corrupt = False

    def persist(self):
        with self.lock:
            if self.corrupt:
                raise ControlError('自动运行状态文件损坏；不会覆盖已有状态。')
            save(self.path, self.data)

    def event(self, message, severity='info'):
        self.data['events'] = (self.data['events'] + [dict(at=self.clock().isoformat(), message=message, severity=severity)])[-100:]
        self.data['message'] = message

    def pause(self, message):
        with self.lock:
            self.data.update(enabled=False, status='paused', generation=uuid4().hex, next_check=None, next_run=None)
            self.event(message, 'error')
            self.persist()

    def disable(self):
        with self.lock:
            if self.corrupt:
                return {'message': self.data['message']}
            self.data.update(enabled=False, status='disabled', generation=uuid4().hex, next_check=None, next_run=None)
            self.event('自动运行已关闭。已提交的委托仍可能成交，可继续核对订单；不会自动撤单。')
            self.persist()
            return {'message': self.data['message']}

    def _pause_after_result(self, message, result):
        self.pause(message)
        evidence = diagnostic_fields(result)
        self.data['last_run'] = dict(at=self.clock().isoformat(), status='paused', message=message, **evidence)
        self.persist()
        return dict(status='paused', message=message, **evidence)

    def reconciled(self, broker_id, result):
        """Close paused orchestration records only after the ledger has no unfinished intents.

        This does not erase a journal, retry an order, or enable automation. A crash
        before the batch was frozen can also produce a clean journal, so do not
        describe this as proof that the portfolio reached its target weights.
        """
        if result.get('status') != 'complete' or result.get('recovery_state') != 'complete':
            return
        with self.lock:
            changed = False
            for cycle in list(self.data['cycles'].values()):
                if cycle['broker_id'] == broker_id and cycle['status'] in {'pending', 'blocked', 'preparing'}:
                    cycle['status'] = 'closed'
                    self._finish_initial(cycle, 'closed')
                    changed = True
            if changed:
                self.event('订单账本已核对，无未完成委托；原自动周期已关闭，不会重新下单。')
                self.persist()

    def configure(self, value):
        if self.data['enabled']:
            raise ControlError('请先关闭自动运行，再修改时间。')
        if any(c['status'] in {'pending', 'blocked', 'preparing'} for c in self.data['cycles'].values()):
            raise ControlError('仍有未完成调仓；先核对订单或结束未提交余单，再修改原执行窗口。')
        try:
            cfg = schedule_config(value)
        except (ValueError, TypeError):
            raise ControlError('时间设置无效；请选择每日、每周或每月，并填写有效的美东时间。') from None
        self.data.update(config=cfg, calendar=None, next_run=None, next_check=None, startup=None)
        self.persist()
        return {'message': '运行时间已保存；尚未开启自动交易。'}

    def available(self, broker_id, source_id):
        issues = []
        try:
            broker = self.registry.descriptor('broker', broker_id)
            source = self.registry.descriptor('data', source_id)
            if not broker['enabled'] or broker['mode'] != 'broker_paper':
                issues.append('自动运行仅支持已绑定的联网模拟账户。')
            if not {'inspect', 'prepare', 'execute', 'calendar', 'reconcile'} <= set(broker['capabilities']):
                issues.append('当前券商未提供完整的交易日历、模拟执行与订单核对能力。')
            if not source['enabled'] or not {'collect', 'paper_signal'} <= set(source['capabilities']):
                issues.append('当前数据源不支持自动采集和模拟信号准备。')
            cfg = self.registry.configuration(broker_id)
            self.registry.compatible(cfg, source_id)
            if not self.registry.control('data', source_id).configuration_status().get('configured', False):
                issues.append('请先配置当前数据源。')
        except Exception:
            issues.append('当前模块配置无法核对。')
        if self.corrupt:
            issues.append(self.data['message'])
        return issues

    def enable(self, broker_id, source_id, verified, *, initialize=False):
        if self.data['enabled']:
            raise ControlError('自动运行已经开启。')
        issues = self.available(broker_id, source_id)
        if not verified and not initialize:
            issues.append('请先检查当前模拟账户连接。')
        if issues:
            raise ControlError(' '.join(issues))
        cfg = self.registry.configuration(broker_id)
        strategy = self.registry.catalog('strategy')[cfg['strategy']['type']]
        binding = dict(broker_id=broker_id, source_id=source_id,
                       broker_name=self.registry.descriptor('broker', broker_id)['name'],
                       source_name=self.registry.descriptor('data', source_id)['name'],
                       strategy_name=strategy.get('display_name', strategy['type']),
                       context=self.registry.context(broker_id, source_id), code_revision=code_revision(self.root))
        # An unfinished older cycle must be resolved, never replaced by a new envelope.
        unfinished = [c for c in self.data['cycles'].values()
                      if c['status'] in {'pending', 'blocked', 'preparing'} and c['broker_id'] == broker_id]
        if unfinished:
            current = self.clock()
            cycle = unfinished[0]
            try:
                resumable = (len(unfinished) == 1 and cycle['status'] == 'pending'
                    and cycle['context'] == binding['context'] and cycle['code_revision'] == binding['code_revision']
                    and cycle['day'] == str(current.astimezone(ZoneInfo('America/New_York')).date())
                    and datetime.fromisoformat(cycle['window_start']) <= current < datetime.fromisoformat(cycle['window_end']))
            except (KeyError, ValueError, TypeError):
                resumable = False
            if not resumable:
                raise ControlError('此账户还有未完成调仓；先核对订单，必要时结束未提交余单。仅原窗口内可恢复同一批次。')
        self.data.update(enabled=True, status='waiting', binding=binding, generation=uuid4().hex,
                         calendar=None, next_check=None, next_run=None)
        if initialize and not unfinished:
            self.data['startup'] = dict(status='pending_check', id=uuid4().hex,
                                       requested_at=self.clock().isoformat())
        elif not unfinished:
            self.data['startup'] = None
        self.event('自动模拟交易已开启；先核对原冻结批次，在原窗口内继续。' if unfinished else
                   '已开启，正在检查账户与数据；首轮建仓将在可用的交易时段开始。' if initialize else
                   '自动模拟交易已开启；先获取交易日历，在计划窗口执行，不立即补下旧订单。')
        self.persist()
        return {'message': self.data['message']}

    def validate_binding(self, broker_id, source_id):
        if not self.data['enabled']:
            return
        b = self.data['binding']
        try:
            matches = (broker_id == b['broker_id'] and source_id == b['source_id']
                       and self.registry.context(broker_id, source_id) == b['context']
                       and code_revision(self.root) == b['code_revision'])
        except Exception:
            matches = False
        if not matches:
            self.pause('模块、账户、策略参数或代码已变化，原自动计划已暂停；核对后重新开启。')

    def public(self, broker_id, source_id):
        issues = self.available(broker_id, source_id)
        value = {k: self.data[k] for k in ('enabled', 'status', 'message', 'config', 'next_run', 'last_run')}
        if not value['enabled']:
            value['next_run'] = None
        startup = self.data.get('startup')
        value['startup'] = {k: startup[k] for k in ('status', 'requested_at', 'checked_at', 'window_start') if k in startup} if startup else None
        b = self.data.get('binding')
        value['binding'] = {k:b[k] for k in ('strategy_name', 'broker_name', 'source_name', 'broker_id', 'source_id') if k in b} if b else None
        value.update(available=not issues, corrupt=self.corrupt, blockers=issues, events=list(reversed(self.data['events'][-30:])))
        return value

    def due(self):
        return (self.data['enabled'] and not self.corrupt and
                (not self.data['next_check'] or self.clock() >= datetime.fromisoformat(self.data['next_check'])))

    def _latest(self):
        b = self.data['binding']
        directory = self.registry.control('data', b['source_id']).directory.resolve()
        raw = json.loads((directory / 'latest.json').read_text(encoding='utf-8'))
        path = project_path(self.root, raw['path'], under='runs')
        if not path.is_relative_to(directory):
            raise ControlError('数据快照索引不属于当前模块。')
        return path

    def _finish_initial(self, cycle, status='complete'):
        if cycle.get('kind') != 'initial':
            return
        startup = self.data.get('startup')
        if not startup or startup.get('id') != cycle.get('startup_id'):
            return
        startup['status'] = 'complete'
        key = cycle['broker_id'] + ':' + cycle.get('period_key', period_key(date.fromisoformat(cycle['day']), self.data['config']))
        # The initial portfolio also satisfies this period's scheduled rebalance.
        self.data['cycles'].setdefault(key, dict(status=status, day=cycle['day'],
            broker_id=cycle['broker_id'], context=cycle['context'], origin='initial'))

    def _startup_check(self, call, sessions, cfg, progress):
        startup = self.data['startup']
        self.data['status'] = 'running'
        self.persist()
        progress('首次启动：检查模拟账户…')
        account = call('status')
        if account.get('status') != 'connected':
            raise ControlError(account['message'])
        progress('首次启动：更新数据并检查选股结果…')
        selection = call('refresh')
        if selection.get('status') != 'previewed':
            raise ControlError(selection['message'])
        current = self.clock()
        start, end = initial_window(current, sessions, cfg)
        local = current.astimezone(ZoneInfo(cfg['timezone']))
        daily = self.data['daily'].setdefault(self.data['binding']['broker_id'] + ':' + str(local.date()),
                                            dict(refresh='new', attempts=0, reconcile='new'))
        daily.update(refresh='complete', attempts=daily['attempts'] + 1)
        startup.update(status='checked', checked_at=current.isoformat(),
                       window_start=start.isoformat(), window_end=end.isoformat())
        message = ('账户、数据与选股已检查，正在准备首轮建仓。' if start <= current < end else
                   '账户、数据与选股已检查；首轮建仓等待下一个美股交易时段。')
        self.data.update(status='waiting', next_check=None, next_run=start.isoformat())
        self.event(message)
        self.persist()
        return dict(status='complete', message=message, startup_check=True,
                    account_check=account, selection=selection)

    def tick(self, worker, progress=lambda message: None):
        """Called with the application's exclusive task reservation; never concurrent with manual work."""
        if not self.due():
            return None
        b, cfg, now = self.data['binding'], self.data['config'], self.clock()
        self.validate_binding(b['broker_id'], b['source_id'])
        if not self.data['enabled']:
            return None
        local = now.astimezone(ZoneInfo(cfg['timezone']))
        today = local.date()
        self.data['next_check'] = (now + timedelta(seconds=cfg['poll_seconds'])).isoformat()

        last_failure = None
        def call(action, snapshot=None, token=None, permit=None):
            nonlocal last_failure
            if not self.data['enabled']:
                raise ControlError('自动运行已关闭。')
            expected = (self.registry.connection_context(b['broker_id']) if action in {'status', 'calendar', 'reconcile'}
                        else b['context'])
            result = worker(self.root, action, snapshot, token, progress, source_id=b['source_id'],
                            broker_id=b['broker_id'], expected_context=expected, automation=permit)
            if result.get('status') in {'blocked', 'paused'}:
                last_failure = result
            return result

        try:
            calendar = self.data.get('calendar')
            if not calendar or calendar['checked_day'] != str(today):
                progress('获取券商交易日历，检查节假日…')
                response = call('calendar')
                if response['status'] != 'ready':
                    raise ControlError(response['message'])
                sessions = [date.fromisoformat(x) for x in response['sessions']]
                if len(set(sessions)) != len(sessions) or not any(d > today for d in sessions):
                    raise ControlError('交易日历覆盖不足，不能计算自动计划。')
                self.data['calendar'] = dict(checked_day=str(today), sessions=[str(d) for d in sessions])
            sessions = [date.fromisoformat(x) for x in self.data['calendar']['sessions']]
            startup = self.data.get('startup')
            if startup and startup['status'] == 'pending_check':
                return self._startup_check(call, sessions, cfg, progress)
            planned = next_window(now, sessions, cfg)
            self.data['next_run'] = planned.isoformat() if planned else None
            cycle_key = b['broker_id'] + ':' + period_key(today, cfg)
            routine_key = period_key(today, cfg)
            try:
                regular_start, regular_end, routine_key = period_window(today, sessions, cfg)
                cycle_key = b['broker_id'] + ':' + routine_key
            except ValueError:
                regular_start = regular_end = instant(today, '00:00', cfg)
            first = bool(startup and startup['status'] == 'checked')
            if first:
                cycle_key = b['broker_id'] + ':initial-' + startup['id']
                start, end = datetime.fromisoformat(startup['window_start']), datetime.fromisoformat(startup['window_end'])
                if local >= end and cycle_key not in self.data['cycles']:
                    start, end = initial_window(now, sessions, cfg)
                    startup.update(window_start=start.isoformat(), window_end=end.isoformat())
                self.data['next_run'] = start.isoformat()
            older = [c for key, c in self.data['cycles'].items()
                     if key != cycle_key and c['broker_id'] == b['broker_id']
                     and c['status'] in {'pending', 'blocked', 'preparing'}]
            if older:
                result = call('reconcile')
                if result.get('status') == 'complete' and result.get('recovery_state') == 'complete':
                    self.reconciled(b['broker_id'], result)
                    self.data['status'] = 'waiting' if self.data['enabled'] else 'disabled'
                    self.persist()
                    return result
                return self._pause_after_result('上一轮调仓仍未完成；已核对订单，本轮不会另建订单。', result)
            if today not in sessions:
                self.data.update(status='waiting', next_check=instant(today+timedelta(days=1),'00:05',cfg).isoformat())
                self.persist()
                return None

            if not first:
                start, end = regular_start, regular_end
            cycle = self.data['cycles'].get(cycle_key)
            in_window = start <= local < end

            if cycle and cycle['status'] in {'pending', 'blocked'}:
                if cycle['status'] == 'blocked':
                    self.pause('上一轮自动调仓仍需人工核对；不会生成新订单替换它。')
                    return {'status':'paused','message':self.data['message']}
                if cycle['context'] != b['context'] or cycle['day'] != str(today) or not in_window:
                    result = call('reconcile')
                    if result.get('status') == 'complete' and result.get('recovery_state') == 'complete':
                        self.reconciled(b['broker_id'], result)
                        self.data['last_run'] = dict(at=self.clock().isoformat(), status='complete', message=self.data['message'])
                        self.data['status'] = 'waiting' if self.data['enabled'] else 'disabled'
                        self.persist()
                        return {'status':'complete','message':self.data['message']}
                    cycle['status'] = 'blocked'
                    return self._pause_after_result('执行窗口已结束，已只读核对订单；未完成部分不会在窗口外补单。', result)
                return self._execute(call, cycle, start, end, recovering=True)

            day_key = b['broker_id'] + ':' + str(today)
            daily = self.data['daily'].setdefault(day_key, dict(refresh='new', attempts=0, reconcile='new'))
            if local >= instant(today, cfg['prepare_time'], cfg) and daily['refresh'] != 'complete':
                if daily['attempts'] >= 3:
                    raise ControlError('今日数据更新已失败三次；自动计划暂停，请检查数据权限或连接。')
                daily.update(refresh='running', attempts=daily['attempts']+1)
                self.data['status'] = 'running'
                self.persist()
                progress('按当前策略更新数据；读取已验证的日期缓存…')
                result = call('refresh')
                if result['status'] != 'previewed':
                    daily['refresh'] = 'failed'
                    self.event(result['message'], 'warning')
                    self.data.update(status='waiting', next_check=(now+timedelta(minutes=15)).isoformat())
                    self.persist()
                    return result
                daily['refresh'] = 'complete'
                self.event('今日策略数据更新完成。')
                self.data.update(status='waiting', next_check=None)
                self.persist()
                return {'status':'complete','message':'自动数据更新完成；将在计划窗口准备模拟订单。'}

            if in_window and not cycle:
                result = call('status')
                if result['status'] != 'connected':
                    raise ControlError(result['message'])
                result = call('prepare', self._latest())
                if result.get('status') == 'complete' and result.get('no_orders'):
                    completed = dict(status='complete', day=str(today), broker_id=b['broker_id'], context=b['context'])
                    if first:
                        completed.update(kind='initial', startup_id=startup['id'], period_key=routine_key)
                    self.data['cycles'][cycle_key] = completed
                    self._finish_initial(completed)
                    self.event('当前持仓已符合目标，无需调仓。')
                    self.data['last_run'] = dict(at=self.clock().isoformat(), status='complete', message=self.data['message'])
                    self.persist()
                    return result
                if not result.get('execution_ready') or not result.get('preview_token'):
                    raise ControlError(result['message'])
                snapshot = project_path(self.root, result['snapshot_path'], under='runs')
                cycle = dict(status='pending', broker_id=b['broker_id'], context=b['context'], day=str(today),
                             snapshot=str(snapshot), token=result['preview_token'], started_at=now.isoformat(),
                             code_revision=b['code_revision'], window_start=start.isoformat(), window_end=end.isoformat())
                if first:
                    cycle.update(kind='initial', startup_id=startup['id'], period_key=routine_key)
                self.data['cycles'][cycle_key] = cycle
                # Save the exact envelope before transmission; a crash can only recover this batch.
                self.persist()
                return self._execute(call, cycle, start, end, recovering=False)

            if local >= end and not cycle:
                self.data['cycles'][cycle_key] = dict(status='missed', day=str(today), broker_id=b['broker_id'], context=b['context'])
                self.event('本期调仓时段已过去，等待下一次计划。', 'warning')
            if local >= instant(today, '16:20', cfg) and daily['reconcile'] != 'complete':
                result = call('reconcile')
                if result['status'] not in {'complete', 'ready'}:
                    raise ControlError(result['message'])
                daily['reconcile'] = 'complete'
                self.event('收盘后订单核对完成。')
                self.persist()
                return result
            self.data['status'] = 'waiting'
            if cycle and cycle['status'] in {'complete', 'closed', 'missed'}:
                future = next_window(end, sessions, cfg)
                self.data['next_run'] = future.isoformat() if future else None
            candidates = [instant(today, cfg['prepare_time'], cfg), instant(today,'16:20',cfg), start,
                          instant(today+timedelta(days=1),'00:05',cfg)]
            self.data['next_check'] = min(t for t in candidates if t > local).isoformat()
            self.persist()
            return None
        except Exception as exc:
            message = str(exc) if isinstance(exc, ControlError) else '自动检查未取得可靠结果，请检查模块连接与运行记录。'
            self.pause(message)
            evidence = (diagnostic_fields(last_failure) if last_failure and isinstance(exc, ControlError)
                        and exc.args == (last_failure.get('message'),) else
                        diagnostic_fields(failure_details(exc, 'automation')))
            self.data['last_run'] = dict(at=self.clock().isoformat(), status='paused', message=message, **evidence)
            self.persist()
            return dict(status='paused', message=message, **evidence)

    def _execute(self, call, cycle, start, end, *, recovering):
        permit = dict(generation=self.data['generation'], context=cycle['context'],
                      window_start=start.isoformat(), window_end=end.isoformat())
        self.data['status'] = 'running'
        self.persist()
        result = call('recover' if recovering else 'execute', cycle['snapshot'], cycle['token'], permit)
        self.data['last_run'] = dict(at=self.clock().isoformat(), status=result['status'], message=result['message'],
                                    **diagnostic_fields(result))
        if result['status'] == 'complete':
            cycle['status'] = 'complete'
            self._finish_initial(cycle)
            self.event('首轮模拟建仓已完成。' if cycle.get('kind') == 'initial' else '本轮模拟调仓已完成。')
            self.data['status'] = 'waiting' if self.data['enabled'] else 'disabled'
        elif result.get('recovery_state') == 'blocked':
            cycle['status'] = 'blocked'
            self.pause(result['message'])
        else:
            # Pending/partial/uncertain first results are queried on the next tick, never re-submitted.
            self.data['status'] = 'waiting' if self.data['enabled'] else 'disabled'
            self.event('本轮委托尚未全部确认成交，继续核对原订单；不会重复发送。', 'warning')
        self.persist()
        return result
