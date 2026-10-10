"""One serialized preparation job composed only of existing read-only worker actions."""
from datetime import datetime, timezone
from pathlib import Path
import threading
from uuid import uuid4

from app.control_contracts import ControlError, project_path
from app.operations import context_for


STEPS = (('account', '检查账户'), ('data', '检查数据配置与权限'),
         ('refresh', '更新所需数据'), ('preview', '生成选股预览'), ('prepare', '核对报价与准备订单'))
READ_ONLY_ACTIONS = frozenset({'status', 'data-status', 'refresh', 'preview', 'prepare'})


def stamp():
    return datetime.now(timezone.utc).isoformat()


def warnings_for(result, metadata=None):
    notes = [str(note) for note in result.get('issues') or []]
    if (metadata or {}).get('collection_complete') is False:
        notes.append('数据并非完整市场覆盖。')
    return list(dict.fromkeys(notes))


def caveats(result, metadata=None):
    notes = warnings_for(result, metadata)
    return f' 存在 {len(notes)} 项数据限制，请查看预览中的数据说明。' if notes else ''


def start_preflight(service):
    """Called with the service lock held. Validation failures become durable step results."""
    service.idle()
    flow = Preflight(service)
    service.clear_preview()
    service.account = None
    service.broker_session.clear()
    service.active = flow.job
    service.jobs = [flow.job, *service.jobs][:40]
    service._save_job(flow.job)
    service.thread = threading.Thread(target=flow.run, name='portal-preflight', daemon=False)
    service.thread.start()
    return {'job_id': flow.job['id']}


class Preflight:
    def __init__(self, service):
        self.service, self.registry = service, service.registry
        self.broker, self.source = service.broker_id, service.source
        self.phase, self.path = 'account', None
        self.issues = []
        self.context, self.binding = None, None
        try:
            self.binding = self.registry.connection_context(self.broker)
        except Exception:
            pass
        try:
            self.context = self.registry.context(self.broker, self.source)
        except Exception:
            pass
        def name(kind, ident):
            try:
                return self.registry.descriptor(kind, ident)['name']
            except Exception:
                return ident
        self.job = dict(id=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8],
                        action='preflight', source=self.source, source_name=name('data', self.source),
                        broker=self.broker, broker_name=name('broker', self.broker),
                        context_version=self.context, status='running', started_at=stamp(),
                        message='开始检查；不会发送订单。', events=[], cancel_available=False,
                        steps=[dict(id=ident, label=label, status='pending', message='等待检查。')
                               for ident, label in STEPS])

    def update(self, phase, status, message, warnings=None):
        with self.service.lock:
            self.phase = phase
            safe = self.service._public(str(message))[:800]
            step = next(s for s in self.job['steps'] if s['id'] == phase)
            step.update(status=status, message=safe)
            if warnings is not None:
                step['warnings'] = self.service._public(warnings)
                self.issues = list(dict.fromkeys([*self.issues, *step['warnings']]))
            self.job.update(message=safe, cancel_available=False)
            self.job['events'] = (self.job['events']+[dict(at=stamp(), message=safe)])[-60:]
            self.service._save_job(self.job)

    def current(self, *, account_only=False):
        with self.service.lock:
            if self.job.get('cancel_requested'):
                raise ControlError('已停止本次检查；未发送订单。')
            if (self.service.closing or self.service.broker_id != self.broker
                    or self.service.source != self.source):
                raise ControlError('检查期间工作台或模块选择发生变化，请重新检查。')
            try:
                valid = (self.binding is not None
                         and self.registry.connection_context(self.broker) == self.binding)
                if not account_only:
                    valid = valid and self.context is not None and self.registry.context(self.broker, self.source) == self.context
            except Exception:
                valid = False
            if not valid:
                self.service.clear_preview()
                self.service.account = None
                self.service.broker_session.clear()
                raise ControlError('模块或配置已改变或无法读取，请检查配置后重新运行检查。')

    def invoke(self, action, *, account_only=False):
        if action not in READ_ONLY_ACTIONS:
            raise ControlError('一键检查不允许执行交易。')
        self.current(account_only=account_only)
        expected = context_for(self.registry, action, self.broker, self.source)
        def progress(message):
            with self.service.lock:
                safe = self.service._public(str(message))[:800]
                step = next(s for s in self.job['steps'] if s['id'] == self.phase)
                step['message'] = safe
                self.job['message'] = safe
                self.job['events'] = (self.job['events']+[dict(at=stamp(), message=safe)])[-60:]
                # Collector clears stale cancellation before its first progress event.
                self.job['cancel_available'] = (action == 'refresh' and not self.job.get('cancel_requested')
                    and 'cancel' in self.registry.descriptor('data', self.source)['capabilities'])
                self.service._save_job(self.job)
        result = self.service.worker(self.service.root, action, self.path, None, progress,
                                     source_id=self.source, broker_id=self.broker,
                                     expected_context=expected, credentials=None)
        self.current(account_only=account_only)
        if not isinstance(result, dict) or not isinstance(result.get('message'), str):
            raise ControlError('检查未取得可核对的结果，请查看配置后重试。')
        if result.get('status') in {'blocked', 'paused'}:
            raise ControlError(result['message'])
        return result

    def publish(self, result, *, authorized=False):
        with self.service.lock:
            self.current()
            safe = self.service._public(result)
            self.service._record_observation('prepare', self.broker, result)
            # A selection preview cannot accidentally become a submission capability.
            if not authorized:
                safe['execution_ready'] = False
            self.service.preview = dict(safe, created_at=stamp(), source=self.source, broker=self.broker)
            self.service.preview_context = self.context
            self.service.preview_private = None
            if authorized and result.get('execution_ready') is True:
                execution_path = result.get('snapshot_path') or self.path
                if not result.get('preview_token') or not execution_path:
                    self.service.clear_preview()
                    raise ControlError('订单准备缺少核验凭据，请重新检查。')
                path = project_path(self.service.root, execution_path, under='runs')
                if not path.is_file():
                    self.service.clear_preview()
                    raise ControlError('订单准备文件不存在，请重新检查。')
                self.service.preview_private = (path, result['preview_token'])
                if safe.get('account_details'):
                    self.service.account = dict(status='connected', message='准备时已核对绑定的账户。',
                                                details=safe['account_details'], checked_at=stamp())

    def pipeline(self):
        self.update('account', 'running', '正在检查所选账户连接…')
        broker = self.registry.descriptor('broker', self.broker)
        if not broker['enabled']:
            raise ControlError('当前账户不可用于账户检查，请选择可用的券商。')
        if 'inspect' not in broker['capabilities']:
            raise ControlError('当前券商未提供账户检查功能。')
        result = self.invoke('status', account_only=True)
        if result.get('status') != 'connected' and not (broker['mode'] == 'paper' and result.get('status') == 'ready'):
            raise ControlError(result['message'] or '尚未连接账户，请先在券商客户端登录。')
        with self.service.lock:
            self.current(account_only=True)
            self.service.account = dict(self.service._public(result), checked_at=stamp())
            if result['status'] == 'connected':
                self.service.broker_session.verify(self.broker, self.binding)
        self.update('account', 'passed', result['message'])

        self.update('data', 'running', '正在检查数据配置与策略需求…')
        source = self.registry.descriptor('data', self.source)
        if not source['enabled']:
            raise ControlError('当前数据模块不可用，请检查数据模块配置。')
        adapter = self.registry.control('data', self.source)
        configured = adapter.configuration_status()
        if configured.get('configured') is False:
            raise ControlError('请先在数据中心配置当前数据源，再运行检查。')
        self.current()
        self.registry.compatible(self.registry.configuration(self.broker), self.source)
        if 'inspect' in source['capabilities']:
            result = self.invoke('data-status')
            if result.get('status') not in {'connected', 'ready'}:
                raise ControlError(result['message'])
        self.update('data', 'passed', '数据配置与权限检查通过。' if 'inspect' in source['capabilities']
                    else '数据配置检查通过；此模块不提供在线权限检查。')

        self.update('refresh', 'running', '正在更新所需数据，复用数据模块已有缓存…')
        if 'collect' in source['capabilities']:
            with self.service.lock:
                self.service.selected_snapshot = None
            result = self.invoke('refresh')
            if result.get('status') != 'previewed' or not result.get('snapshot_path'):
                raise ControlError('数据更新未生成完整的可用快照，请查看数据说明。')
            self.path = project_path(self.service.root, result['snapshot_path'], under='runs')
            with self.service.lock:
                meta = self.service._register_snapshot(self.path, self.source)
            self.update('refresh', 'passed', result['message'] + caveats(result, meta), warnings_for(result, meta))
        else:
            with self.service.lock:
                self.current()
                meta = self.service.snapshot()
                if not meta:
                    raise ControlError('此数据源使用文件输入；请先在数据中心导入完整快照。')
                self.path = self.service.snapshots[meta['id']]
            self.update('refresh', 'skipped', '已使用导入的完整快照；此数据源不提供在线更新。')
        self.current()

        self.update('preview', 'running', '正在根据当前策略生成目标组合…')
        result = self.invoke('preview')
        if result.get('status') != 'previewed':
            raise ControlError('未取得有效的选股预览。')
        self.publish(result)
        self.update('preview', 'passed', result['message'] + caveats(result), warnings_for(result))

        self.update('prepare', 'running', '正在核对交易时段、账户持仓、报价与订单数量…')
        if broker['mode'] == 'paper':
            # Local replay uses the normal preview path; never claim live quotes.
            if result.get('execution_ready') is not True:
                raise ControlError((result.get('issues') or ['当前快照尚未满足本地回放条件。'])[0])
        else:
            if 'prepare' not in broker['capabilities']:
                raise ControlError('当前券商不提供订单准备功能，请更换支持此功能的模块。')
            with self.service.lock:
                self.service.broker_session.require(self.broker, self.binding)
            result = self.invoke('prepare')
        if result.get('status') == 'complete' and result.get('no_orders') is True:
            self.publish(result)
            self.update('prepare', 'passed', result['message'] + caveats(result), warnings_for(result))
            return dict(result, message='检查完成：已匹配目标或差额不足一股，无需提交订单。')
        if result.get('status') != 'previewed' or result.get('execution_ready') is not True:
            self.publish(result)
            raise ControlError((result.get('blockers') or [result['message']])[0])
        self.publish(result, authorized=True)
        self.update('prepare', 'passed', '订单已准备，尚未发送；请核对后确认。' + caveats(result), warnings_for(result))
        return dict(result, message='检查通过，订单待确认。' + caveats(result))

    def run(self):
        try:
            result = self.pipeline()
            with self.service.lock:
                self.current()
        except ControlError as exc:
            result = dict(status='blocked', message=str(exc), issues=self.issues, automatic_retry=False)
        except Exception:
            # No raw vendor exception or traceback reaches persisted/public logs.
            result = dict(status='blocked', message='检查未完成，请检查当前步骤的模块配置与连接；不会自动重试。', issues=self.issues, automatic_retry=False)
        with self.service.lock:
            if result['status'] == 'blocked':
                self.service.preview_private = None
                if self.service.preview:
                    self.service.preview['execution_ready'] = False
                if self.phase == 'account':
                    self.service.account = None
                    self.service.broker_session.clear()
                self.update(self.phase, 'blocked', result['message'])
                for step in self.job['steps']:
                    if step['status'] == 'pending':
                        step.update(status='skipped', message='前置检查未通过，尚未执行。')
            safe = self.service._public(result)
            self.job.update(status=safe['status'], message=safe['message'], result=safe,
                            finished_at=stamp(), cancel_available=False)
            self.service._save_job(self.job)
            self.service.active = None
