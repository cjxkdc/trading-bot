"""One operation against selected modules. No provider-specific dispatch or live session."""
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.assembly import build_portfolio_runner
from app.control_contracts import ControlError, project_path
from data.common import DataIssue, write_json
from framework.decisions import digest
from framework.execution import ExecutionBlocked
from framework.order_capabilities import order_capability_issues
from framework.trading import fresh
from app.module_registry import ModuleRegistry

ROOT = Path(__file__).resolve().parents[1]
from app.operations import ACTIONS, context_for


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
    if mode == 'broker_paper':
        if raw.get('synthetic', False) is not False or any(str(s.ticker).startswith('DEMO-') for s in loaded.stocks):
            issues.append('合成数据不能提交到联网券商。')
        if snapshot.execution_time.astimezone(ZoneInfo('America/New_York')).date() != clock.astimezone(ZoneInfo('America/New_York')).date():
            issues.append('快照不是当前美股交易日的数据。')
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
    if automation is not None:
        from app.automation import submission_guard
        if not isinstance(automation, dict) or set(automation) != {'generation', 'context', 'window_start', 'window_end'}:
            raise ControlError('自动运行授权格式无效。')
        before_submit = lambda: submission_guard(root, automation)
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
    if action in {'calendar', 'reconcile', 'resolve-remainder'}:
        control = registry.control('broker', broker_id)
        descriptor = registry.descriptor('broker', broker_id)
        capability = 'reconcile' if action == 'resolve-remainder' else action
        if descriptor['mode'] != 'broker_paper' or capability not in descriptor['capabilities']:
            raise ControlError('当前模拟账户模块不支持此操作。')
        if action == 'calendar':
            from datetime import timedelta
            today = clock.astimezone(ZoneInfo('America/New_York')).date()
            sessions = control.trading_sessions(today.replace(day=1) - timedelta(days=7), today + timedelta(days=100))
            return {'status':'ready','message':'交易日历已更新。','sessions':[str(day) for day in sessions]}
        from app.paper_recovery import reconcile_orders, close_unsubmitted
        runner = build_portfolio_runner(registry.configuration(broker_id))
        if action == 'resolve-remainder':
            automation_path = root / 'runs/automation/state.json'
            if automation_path.exists() and json.loads(automation_path.read_text(encoding='utf-8')).get('enabled') is not False:
                raise ControlError('请先关闭自动运行，再处理未提交余单。')
            return close_unsubmitted(control=control, policy=runner.policy, fees=runner.fees)
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
    if action == 'prepare' and (broker['mode'] != 'broker_paper' or 'prepare' not in broker['capabilities']):
        raise ControlError('当前账户模块没有模拟订单准备能力。')
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
    from app.paper_preparation import SCHEMA as PREPARED, prepare, execute_prepared
    if automation is not None and action in {'execute', 'recover'} and raw.get('schema') != PREPARED:
        raise ControlError('自动执行只接受已经冻结并核对过的模拟订单预览。')
    if action in {'execute', 'recover'} and raw.get('schema') == PREPARED:
        if broker['mode'] != 'broker_paper' or 'prepare' not in broker['capabilities']:
            raise ControlError('当前账户不支持这份模拟订单预览。')
        method = execute_prepared
        if action == 'recover':
            from app.paper_recovery import recover_prepared
            method = recover_prepared
        return method(root=root,raw=raw,context=context,token=preview_token,source=source,
                      runner=runner,control=registry.control('broker',broker_id),cfg=cfg,clock=clock,
                      before_submit=before_submit)
    if action == 'recover':
        raise ControlError('只能核对已有的冻结模拟订单批次。')
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
        decision = runner.plan(loaded.execution)
        token = digest({'context': context, 'snapshot': raw, 'decision': decision.record})
        issues = execution_issues(loaded, decision, runner.policy, clock, broker['mode'])
        _, broker_entry = registry.resolve('broker', broker_id)
        issues.extend(order_capability_issues(broker_entry['control'].get('order_capabilities'), loaded.order_requirements))
        if 'execute' not in broker['capabilities']:
            issues.append('此账户模块未声明执行能力。')
        if action == 'execute':
            if not preview_token or preview_token != token:
                raise ControlError('数据、策略、券商或配置已变化，请重新预览。')
            if issues:
                raise ControlError(' '.join(issues))
            control = registry.control('broker', broker_id)
            with control.session(cfg, loaded.execution, runner.fees) as (connection, journal):
                if connection.mode != broker['mode'] or connection.mode not in {'paper', 'broker_paper'}:
                    raise ControlError('实际账户环境与所选模块不一致。')
                detail = runner.execute(decision, loaded.execution, connection, journal)
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
    except (ControlError, DataIssue) as exc:
        result = {'status': 'paused' if action == 'execute' else 'blocked', 'message': str(exc)[:1200]}
    except ExecutionBlocked:
        result = {'status': 'paused', 'message': '执行暂停，请保留账本并核对订单；不会自动重试。'}
    except Exception:
        # Third-party exceptions may contain credentials. Do not echo arbitrary SDK output.
        result = {'status': 'paused' if action == 'execute' else 'blocked',
                  'message': '模块操作未通过检查，请核对当前模块的配置、数据格式和账户绑定。'}
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
