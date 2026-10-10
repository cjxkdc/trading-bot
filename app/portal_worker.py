"""One operation against selected modules. Capabilities and bound environment determine execution."""
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.assembly import build_portfolio_runner
from app.control_contracts import ControlError, project_path
from data.common import DataIssue, describe_issue, write_json
from framework.decisions import digest
from framework.contracts import SubmissionRejected
from framework.execution import ExecutionBlocked
from framework.order_capabilities import order_capability_issues, OrderRequirements
from framework.trading import fresh
from app.module_registry import ModuleRegistry
from app.diagnostics import failure_details

ROOT = Path(__file__).resolve().parents[1]
from app.operations import ACTIONS, LEDGER_CLOSURES, context_for

# The framework raises fixed English literals (never broker text). Map them to
# an instruction the user can act on; unknown literals keep the generic notice.
BLOCKED_REASONS = {
    'Earlier cycle has unfinished orders': '上一个交易日的批次仍有未完成委托，请先核对订单，再执行新一轮。',
    'Broker has unresolved orders': '券商账户仍有未完成委托，请先核对订单。',
    'Broker still reports other unresolved orders': '券商账户仍有其他未完成委托，请先核对。',
    'Account changed after order preview; prepare again': '账户在准备订单后发生了变化，请重新准备订单。',
    'Cycle already frozen with different decision/configuration': '同一交易日已冻结另一批次，策略或配置不同；今天不能以不同配置再次执行。',
    'Decision belongs to another execution session': '订单预览属于另一个交易日，请重新准备订单。',
    'Decision was changed after signing': '订单预览在签名后被修改，请重新准备订单。',
    'Trading session changed; no further orders submitted': '交易时段已变化，剩余委托未提交；下个交易日请先核对订单。',
    'Inconsistent broker order state': '券商返回的订单状态与账本不一致，请人工核对后再处理。',
    'Broker fill violates the limit price': '券商成交价越过了限价，请人工核对该委托。',
    'Account differs from recorded fills; automatic continuation paused': '账户现金或持仓与已记录成交不符（手续费或其他操作可能影响现金），自动续行已暂停，请核对。',
    'Intent already claimed by another execution': '另一个执行进程正在处理该委托，请稍后核对订单。',
    'Unknown, pending or partial orders must be reconciled before closing': '仍有状态不明、等待成交或部分成交的委托，请先核对再关闭。',
    'Unresolved broker orders prevent closing unsubmitted intents': '券商仍有未完成委托，不能结束未提交余单。',
    'No cancelled or rejected order needs closing': '没有需要关闭的撤单或拒单。',
    'No untransmitted unknown intent needs closing': '没有需要关闭的未送达委托。',
    'Broker evidence does not prove the intent was never transmitted': '券商记录不能证明该委托从未送达，不能关闭；请继续核对。',
    'This broker adapter cannot prove that an intent was never transmitted': '当前券商模块不支持「确认未发送」，请在券商端处理。',
    'Frozen portfolio context differs from the account holdings': '账户持仓与冻结的策略上下文不一致，请重新预览并准备。',
    'Recovery orders differ from the frozen batch': '待续行的订单与冻结批次不一致，请核对账本。',
}
TRADING_REASONS = {
    'Execution requires a marketable limit': '报价已变化，限价不再可成交；请重新准备订单。',
    'Stale or future account/quote': '账户或报价的时间戳已过期，请重新准备订单。',
    'Insufficient cash including fees': '含手续费后现金不足，请重新准备订单。',
    'Post-fee exposure exceeds limit': '下单后仓位将超过限制，请重新准备订单。',
    'Targets exceed execution policy': '策略目标权重超过执行策略限制，请检查参数。',
}


def blocked_message(exc):
    text = str(exc)
    if text in BLOCKED_REASONS:
        return BLOCKED_REASONS[text] + '不会自动重试。'
    if text.startswith('Submission not attempted'):
        return '发送前检查未通过或券商查询失败，该委托未发送、仍保留在计划中；核对账户后可继续执行。'
    if text.startswith('Order ') and 'reconcile before continuing' in text:
        return '有委托尚未完成（' + text.split(':')[0][len('Order '):] + '），请先核对订单；不会重发。'
    return '执行暂停，请保留账本并核对订单；不会自动重试。'


def research_preview(cfg, runner, loaded, path):
    assumptions = []
    if getattr(runner.engine.strategy, 'requires_context', False):
        method = getattr(runner.engine.strategy, 'research_targets', None)
        note = getattr(runner.engine.strategy, 'research_preview_note', '')
        if not callable(method) or not note:
            raise ControlError('当前研究快照未提供此策略需要的账户上下文。')
        targets = tuple(method(loaded.stocks))
        assumptions.append(note)
    else:
        targets = tuple(runner.engine.strategy.select(loaded.stocks))
    if (len(loaded.labels) != len(loaded.stocks) or len({t.asset_id for t in targets}) != len(targets)
            or any(t.asset_id not in loaded.labels or not isfinite(t.weight) or not isfinite(t.score)
                   or not 0 < t.weight <= runner.engine.max_weight + 1e-12 for t in targets)
            or sum(t.weight for t in targets) > runner.engine.max_gross + 1e-12):
        raise ControlError('策略输出没有通过框架的仓位检查。')
    meta = loaded.metadata
    return {'status': 'previewed', 'message': f"研究预览完成：有效 {len(loaded.stocks)} / 候选 {meta['candidate_count']}。",
            'execution_ready': False, 'snapshot_path': str(path),
            'targets': [dict(asset_id=t.asset_id, weight=t.weight, score=t.score, **loaded.labels[t.asset_id]) for t in targets],
            'issues': [*meta.get('limitations', []), *assumptions],
            'details': dict(meta, strategy=cfg['strategy']['type'], execution_allowed=False,
                            assumptions=assumptions)}


def execution_issues(loaded, decision, policy, clock, mode):
    raw, snapshot = loaded.raw, loaded.execution
    issues = []
    if mode in {'broker_paper','live'}:
        if raw.get('synthetic', False) is not False or any(str(s.ticker).startswith('DEMO-') for s in loaded.stocks):
            issues.append('合成数据不能提交到联网券商。')
        if snapshot.execution_time.astimezone(ZoneInfo(snapshot.market.timezone)).date() != clock.astimezone(ZoneInfo(snapshot.market.timezone)).date():
            issues.append('快照不是当前市场交易日的数据。')
    if not loaded.stocks:
        issues.append('股票池为空。')
    if not snapshot.quotes or any(t.asset_id not in snapshot.quotes for t in decision.targets):
        issues.append('缺少目标股票的买卖报价。')
    try:
        for quote in snapshot.quotes.values():
            fresh(quote.as_of, snapshot.execution_time if mode == 'paper' else clock, policy.max_quote_age_seconds)
            if quote.bid > quote.ask:
                raise ValueError('crossed')
    except ValueError:
        issues.append('报价不符合当前运行时钟或买卖价格校验，请重新获取数据。')
    return issues



def require_snapshot_context(loaded, broker, control=None):
    from brokers.controls import require_account_snapshot, snapshot_instruments
    if not isinstance(loaded.order_requirements, OrderRequirements):
        raise ControlError('数据接入未声明证券与订单要求，无法准备或执行订单。')
    trusted = None if control is None else control.instrument_rules(snapshot_instruments(loaded.execution))
    require_account_snapshot(loaded.execution, broker['execution_context'], loaded.order_requirements,
                             trusted_rules=trusted)


def perform(action, *, root=ROOT, broker_id, source_id, expected_context=None, snapshot_path=None,
            preview_token=None, credentials=None, progress=None, clock=None, automation=None):
    if action not in ACTIONS:
        raise ControlError('未知工作台操作。')
    root = Path(root).resolve()
    registry = ModuleRegistry(root)
    context = context_for(registry, action, broker_id, source_id)
    if expected_context is not None and expected_context != context:
        raise ControlError('模块或配置在任务开始前发生变化，请重新操作。')
    clock = clock or datetime.now(timezone.utc)
    progress = progress or (lambda message: None)
    before_submit = None
    if action in {'execute', 'resolve-remainder', 'close-terminal', 'close-untransmitted'}:
        from app.strategy_instances import require_unmanaged
        require_unmanaged(root, registry, broker_id)
        before_submit = lambda: require_unmanaged(root, registry, broker_id)
    if automation is not None:
        from app.automation import submission_guard
        if not isinstance(automation, dict) or set(automation) != {'generation', 'context', 'window_start', 'window_end'}:
            raise ControlError('自动运行授权格式无效。')
        manual_guard = before_submit
        def before_submit():
            if manual_guard:
                manual_guard()
            submission_guard(root, automation)
    if action == 'observe':
        if 'observe' not in registry.descriptor('broker', broker_id)['capabilities']:
            raise ControlError('当前账户模块尚未提供估值接口。')
        observation = registry.control('broker', broker_id).financial_observation()
        try:
            cfg = registry.configuration(broker_id)
            source = registry.control('data', source_id, bind_request=False)
            benchmark = getattr(source, 'benchmark_observation', None)
            if callable(benchmark):
                observation['benchmark'] = benchmark(cfg.get('benchmark', 'QQQ'), clock=clock)
        except Exception:
            observation.setdefault('limitations', []).append('基准数据暂不可用；账户记录仍已取得。')
        return dict(status='complete', message='账户表现已更新。', observation=observation)
    if action in {'calendar', 'reconcile'} | LEDGER_CLOSURES:
        control = registry.control('broker', broker_id)
        descriptor = registry.descriptor('broker', broker_id)
        capability = 'reconcile' if action in LEDGER_CLOSURES else action
        if capability not in descriptor['capabilities']:
            raise ControlError('当前账户模块不支持此操作。')
        if action == 'calendar':
            from datetime import timedelta
            today = clock.astimezone(ZoneInfo(descriptor['execution_context']['timezone'])).date()
            sessions = control.trading_sessions(today.replace(day=1) - timedelta(days=7), today + timedelta(days=100))
            return {'status':'ready','message':'交易日历已更新。','sessions':[day if isinstance(day, dict) else str(day) for day in sessions], 'execution_context':descriptor['execution_context']}
        from app.paper_recovery import reconcile_orders, close_unsubmitted, close_terminal, close_untransmitted
        runner = build_portfolio_runner(registry.configuration(broker_id))
        if action in LEDGER_CLOSURES:
            automation_path = root / 'runs/automation/state.json'
            if automation_path.exists() and json.loads(automation_path.read_text(encoding='utf-8')).get('enabled') is not False:
                raise ControlError('请先关闭自动运行，再处理未提交余单。')
            closing = {'close-terminal': close_terminal, 'resolve-remainder': close_unsubmitted,
                       'close-untransmitted': close_untransmitted}[action]
            return closing(control=control, policy=runner.policy, fees=runner.fees)
        return reconcile_orders(control=control, policy=runner.policy, fees=runner.fees)
    if action in {'offline', 'status'}:
        broker = registry.descriptor('broker', broker_id)
        if 'inspect' not in broker['capabilities']:
            raise ControlError('此账户模块未声明连接检查能力。')
        control = registry.control('broker', broker_id)
        result = control.inspect(connect=action == 'status')
        connected = not result.get('issues') and result.get('status') == 'connected'
        ok = not result.get('issues')
        return {'status': 'connected' if connected else 'ready' if ok else 'blocked',
                'message': '账户检查通过。' if connected else result.get('message', '本地配置已检查，尚未取得账户数据。') if ok else '账户配置未通过检查。',
                'details': result, 'broker_id': broker_id}
    capabilities = registry.descriptor('data', source_id)['capabilities']
    if action == 'configure-data':
        if 'configure' not in capabilities or not isinstance(credentials, dict):
            raise ControlError('此数据模块不支持网页配置。')
        result = registry.control('data', source_id, bind_request=False).configure(credentials)
        return dict(result, status='ready')
    cfg = registry.configuration(broker_id)
    broker = registry.descriptor('broker', broker_id)
    source = registry.control('data', source_id, config=cfg)
    if action == 'preview' and 'preview' not in capabilities:
        raise ControlError('此数据模块未声明预览能力。')
    if action == 'execute' and 'execute' not in broker['capabilities']:
        raise ControlError('此账户模块未声明执行能力。')
    if action == 'prepare' and ('prepare' not in broker['capabilities']):
        raise ControlError('当前账户模块没有订单准备能力。')
    if action in {'prepare','execute','recover'}:
        registry.require_execution(broker_id)
    registry.compatible(cfg, source_id)
    runner = build_portfolio_runner(cfg)
    required = runner.engine.strategy.history_requirements
    bars = max(required.price_bars, required.dollar_volume_bars)
    if action == 'data-status':
        if 'inspect' not in capabilities:
            raise ControlError('此数据模块未提供在线权限检查。')
        result = source.inspect(price_bars=bars, progress=progress)
        return {'status': 'connected', 'message': '数据接口检查通过。', 'details': result}
    if action == 'refresh':
        if 'collect' not in capabilities:
            raise ControlError('此模块使用文件输入，不支持在线采集。')
        path = source.collect(source.directory / (clock.strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:8]),
                              price_bars=bars, progress=progress)
        snapshot_path = project_path(root, path, under='runs')
    elif not snapshot_path:
        raise ControlError('请先下载或导入与数据模块匹配的快照。')
    path = project_path(root, snapshot_path, under='runs')
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ControlError('快照超过读取上限。')
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    from app.execution_preparation import SCHEMA as PREPARED, LEGACY_SCHEMA, prepare, execute_prepared
    if automation is not None and action in {'execute', 'recover'} and raw.get('schema') not in {PREPARED,LEGACY_SCHEMA}:
        raise ControlError('自动执行只接受已经冻结并核对过的订单预览。')
    if action in {'execute', 'recover'} and raw.get('schema') in {PREPARED,LEGACY_SCHEMA}:
        if 'prepare' not in broker['capabilities']:
            raise ControlError('当前账户不支持这份订单预览。')
        method = execute_prepared
        if action == 'recover':
            from app.paper_recovery import recover_prepared
            method = recover_prepared
        return method(root=root,raw=raw,context=context,token=preview_token,source=source,
                      runner=runner,control=registry.control('broker',broker_id),cfg=cfg,clock=clock,
                      before_submit=before_submit)
    if action == 'recover':
        raise ControlError('只能核对已有的冻结订单批次。')
    loaded = source.load(raw)
    if loaded.schema != runner.input_schema:
        raise ControlError('数据模块返回的输入格式与策略不兼容。')
    if loaded.research_only:
        if action == 'execute':
            raise ControlError('研究数据不能进入执行接口。')
        if action == 'prepare':
            try:
                result = prepare(root=root,source=source,loaded=loaded,runner=runner,
                                 control=registry.control('broker',broker_id),path=path,context=context,
                                 clock=clock,progress=progress)
            except ControlError as exc:
                result = research_preview(cfg,runner,loaded,path)
                result.update(message=str(exc),preparation_attempted=True,
                              blockers=[str(exc)],issues=[str(exc),*result['issues']])
        else:
            result = research_preview(cfg, runner, loaded, path)
    else:
        control = registry.control('broker', broker_id)
        require_snapshot_context(loaded, broker, control)
        decision = runner.plan(loaded.execution)
        token = digest({'context': context, 'snapshot': raw, 'decision': decision.record})
        issues = execution_issues(loaded, decision, runner.policy, clock, broker['mode'])
        _, broker_entry = registry.resolve('broker', broker_id)
        issues.extend(order_capability_issues(broker_entry['control'].get('order_capabilities'), loaded.order_requirements))
        if not broker.get('execution_enabled', True):
            issues.append(broker.get('execution_reason') or '此账户尚未获得执行权限。')
        if 'execute' not in broker['capabilities']:
            issues.append('此账户模块未声明执行能力。')
        if action == 'execute':
            if not preview_token or preview_token != token:
                raise ControlError('数据、策略、券商或配置已变化，请重新预览。')
            if issues:
                raise ControlError(' '.join(issues))
            control = registry.control('broker', broker_id)
            with control.session(cfg, loaded.execution, runner.fees) as (connection, journal):
                if connection.mode != broker['mode'] or connection.mode not in {'paper', 'broker_paper','live'}:
                    raise ControlError('实际账户环境与所选模块不一致。')
                def submit_guard():
                    registry.require_execution(broker_id)
                    if before_submit:
                        before_submit()
                detail = runner.execute(decision, loaded.execution, connection, journal, enable_live=connection.mode == 'live',
                                        before_submit=submit_guard)
            result = {'status': 'complete', 'message': '本轮执行结束；未启动持续调度。',
                      'details': {k: v for k, v in detail.items() if k != 'account'}}
        else:
            result = {'status': 'previewed', 'message': '目标组合预览完成，尚未发送订单。',
                      'snapshot_path': str(path), 'preview_token': token, 'execution_ready': not issues,
                      'expires_at': None if broker['mode'] == 'paper' or not loaded.execution.quotes else min(q.as_of.timestamp() for q in loaded.execution.quotes.values()) + runner.policy.max_quote_age_seconds,
                      'issues': issues, 'targets': [dict(t, **loaded.labels[t['asset_id']]) for t in decision.record['targets']],
                      'details': dict(loaded.metadata, strategy=cfg['strategy']['type'])}
    if action == 'refresh':
        write_json(source.directory / 'latest.json', {'path': str(path)})
    return result


def dispatch(action, **kwargs):
    try:
        result = perform(action, **kwargs)
    except DataIssue as exc:
        result = dict(failure_details(exc, action), status='paused' if action == 'execute' else 'blocked',
                      message=describe_issue(str(exc))[:1200])
    except ControlError as exc:
        result = dict(failure_details(exc, action), status='paused' if action == 'execute' else 'blocked', message=str(exc)[:1200])
    except ExecutionBlocked as exc:
        result = dict(failure_details(exc, action), status='paused', message=blocked_message(exc))
    except Exception as exc:
        # Third-party exceptions may contain credentials. Do not echo arbitrary SDK output.
        result = dict(failure_details(exc, action), status='paused' if action == 'execute' else 'blocked')
        if isinstance(exc, ValueError) and str(exc) in TRADING_REASONS:
            result['message'] = TRADING_REASONS[str(exc)]
        if isinstance(exc, SubmissionRejected):
            result['recovery_state'] = 'blocked'
    if action == 'recover' and result.get('status') in {'blocked', 'paused'} and 'recovery_state' not in result:
        # Known pending/partial results carry waiting; every other recovery failure
        # requires inspection, rather than a repeated attempt with unsafe inputs.
        result['recovery_state'] = 'blocked'
    return dict(result, action=action, automatic_retry=False, portal_protocol=1)


def main():
    try:
        request = json.loads(sys.stdin.read(16385))
        if not isinstance(request, dict) or set(request) - {'action', 'broker_id', 'source_id', 'expected_context',
                                                          'snapshot_path', 'preview_token', 'credentials', 'automation'}:
            raise ValueError('request')
        action = request.pop('action')
        def progress(message):
            print(json.dumps({'portal_progress': 1, 'message': str(message)}, ensure_ascii=False), flush=True)
        result = dispatch(action, progress=progress, **request)
    except Exception:
        result = {'portal_protocol': 1, 'action': None, 'status': 'blocked', 'message': '工作请求无效。'}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 2 if result['status'] in {'blocked', 'paused'} else 0


if __name__ == '__main__':
    sys.exit(main())
