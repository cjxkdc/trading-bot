"""Compose a current signal with a read-only paper account and executable quotes."""
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.control_contracts import ControlError, project_path
from data.common import write_json
from framework.decisions import digest
from framework.order_capabilities import order_capability_issues
from framework.journal import packed, unpack_order
from framework.portfolio import PortfolioContext
from framework.runtime import MarketSnapshot
from framework.trading import Account, Quote, amount, plan_orders, fresh

SCHEMA = 'paper_order_preparation_v1'
NY = ZoneInfo('America/New_York')


def mapping_for(stocks, account):
    mapping = {s.asset_id:s.ticker for s in stocks}
    if len(mapping) != len(stocks) or len(set(mapping.values())) != len(mapping):
        raise ControlError('股票标识和代码映射重复。')
    reverse = {ticker:asset for asset,ticker in mapping.items()}
    for ticker, quantity in account.positions:
        if ticker not in reverse:
            asset = 'HELD:US:' + ticker
            if asset in mapping:
                raise ControlError('持仓证券标识冲突。')
            mapping[asset] = ticker
            reverse[ticker] = asset
    mapped = replace(account, positions=tuple(sorted((reverse[t],q) for t,q in account.positions)))
    return mapping, mapped


def market_snapshot(stocks, account, mapping, quotes, stamp, schema, clock_offset=0.):
    context = PortfolioContext(stamp.astimezone(NY).date(), tuple(s for s,q in account.positions if q))
    return MarketSnapshot(schema, stocks, quotes, stamp, context, True, mapping, clock_offset)


def require_orders(control, loaded):
    issues = order_capability_issues(getattr(control, 'entry', {}).get('control', {}).get('order_capabilities'), loaded.order_requirements)
    if issues:
        raise ControlError(' '.join(issues))


def prepare(*, root, source, loaded, runner, control, path, context, clock, progress):
    if not callable(getattr(source,'paper_signal',None)) or not callable(getattr(control,'observe_account',None)):
        raise ControlError('当前数据或账户模块尚未声明模拟订单准备能力。')
    require_orders(control, loaded)
    stocks, warnings = source.paper_signal(loaded.raw,clock)
    progress('读取绑定的模拟账户持仓，并按持仓重新选股…')
    mapping, account = mapping_for(stocks,control.observe_account())
    preliminary = market_snapshot(stocks,account,mapping,{},datetime.now(timezone.utc),loaded.schema)
    decision = runner.plan(preliminary)
    if not decision.targets and not getattr(runner, 'allow_cash_target', False):
        raise ControlError('本轮没有可交易的目标组合；请先核对选股结果。')
    wanted = {t.asset_id for t in decision.targets} | {s for s,q in account.positions if q}
    progress('读取目标股票与现有持仓的券商行情，核对交易时段和价格日期…')
    quotes, previous, stamp = control.observe_quotes({s:mapping[s] for s in sorted(wanted)},runner.policy.max_quote_age_seconds)
    if any(s.price_as_of != previous for s in stocks):
        raise ControlError(f'信号数据应截至最近已完成交易日 {previous}，请先更新数据。')
    offset = (stamp-datetime.now(timezone.utc)).total_seconds()
    account = replace(account,as_of=account.as_of+timedelta(seconds=offset))
    snapshot = market_snapshot(stocks,account,mapping,quotes,stamp,loaded.schema,offset)
    decision = runner.plan(snapshot)
    progress('计算现金覆盖的整数股订单，按单笔金额上限拆分…')
    try:
        orders = plan_orders(decision,account,quotes,stamp,runner.policy,runner.fees,split_orders=True,tick_size=Decimal('.01'))
    except ValueError:
        raise ControlError('持仓、现金、报价或订单金额未通过执行检查，请核对账户与仓位限制。') from None
    if not orders:
        desired = {t.asset_id: int((account.cash + sum(q*quotes[s].mark for s,q in account.positions))
                                   * Decimal(str(t.weight)) / quotes[t.asset_id].ask) for t in decision.targets}
        if 'desired_positions' in decision.record:
            desired = {s: int(q) for s, q in decision.record['desired_positions'].items()}
        held = dict(account.positions)
        if any(desired.get(s, 0) != held.get(s, 0) for s in set(desired) | set(held)):
            raise ControlError('目标仍有整股差额，但现金或仓位限制不允许下单；本轮已暂停。')
        return {'status':'complete', 'no_orders':True, 'execution_ready':False,
                'preparation_attempted':True, 'orders':[], 'decision_id':decision.decision_id,
                'targets':[dict(asset_id=t.asset_id,weight=t.weight,score=t.score,**loaded.labels[t.asset_id]) for t in decision.targets],
                'message':'当前持仓已匹配目标或差额不足一股，本轮无需提交订单。', 'issues':warnings}
    envelope = json.loads(packed({
        'schema':SCHEMA,'context':context,'source_snapshot':str(path),
        'source_sha256':digest(loaded.raw),'execution_time':stamp.isoformat(),
        'clock_offset_seconds':offset,
        'account':asdict(account),'instruments':mapping,
        'quotes':[asdict(q) for q in quotes.values()],
        'orders':[asdict(o) for o in orders], 'decision_id':decision.decision_id,
    }))
    token = digest(envelope)
    output = Path(root)/'runs/portal/execution'/f'{uuid4().hex}.json'
    write_json(output,envelope)
    return {'status':'previewed','message':f'模拟订单预览完成：{len(decision.targets)} 只股票、{len(orders)} 笔委托，尚未发送订单。',
            'decision_id':decision.decision_id,
            'execution_ready':True,'preparation_attempted':True,'snapshot_path':str(output),'preview_token':token,
            'targets':[dict(asset_id=t.asset_id,weight=t.weight,score=t.score,**loaded.labels[t.asset_id]) for t in decision.targets],
            'orders':[{'ticker':mapping[o.symbol],'side':o.side,'quantity':str(o.quantity),
                       'limit_price':str(o.limit_price),'notional':str(o.quantity*o.limit_price)} for o in orders],
            'account_details':{'account_environment':'SIMULATE','currency':account.currency,'cash':str(account.cash),
                               'positions':[{'symbol':mapping[s],'quantity':str(q)} for s,q in account.positions],
                               'open_order_count':0},
            'issues':warnings,'expires_at':(datetime.now(timezone.utc).timestamp()+runner.policy.max_quote_age_seconds),
            'details':dict(loaded.metadata,information_mode='current_observation',execution_allowed=True)}


def execute_prepared(*, root, raw, context, token, source, runner, control, cfg, clock,
                     before_submit=None):
    return _execute_prepared(root=root, raw=raw, context=context, token=token, source=source,
                             runner=runner, control=control, cfg=cfg, clock=clock,
                             before_submit=before_submit)


def _execute_prepared(*, root, raw, context, token, source, runner, control, cfg, clock,
                      before_submit=None, recover=False):
    if not token or raw.get('context') != context or digest(raw) != token:
        raise ControlError('订单预览或模块已经变化，请重新准备交易。')
    path = project_path(root,raw['source_snapshot'],under='runs')
    if path.stat().st_size > 32*1024*1024:
        raise ControlError('信号文件超过读取上限。')
    signal = json.loads(path.read_text(encoding='utf-8-sig'))
    if digest(signal) != raw['source_sha256']:
        raise ControlError('信号文件在预览后发生变化，请重新准备交易。')
    loaded = source.load(signal)
    require_orders(control, loaded)
    stocks, _ = source.paper_signal(signal,clock)
    stamp = datetime.fromisoformat(raw['execution_time'])
    if recover:
        from app.paper_recovery import reconcile_orders
        report = reconcile_orders(control=control, policy=runner.policy, fees=runner.fees)
        if report['recovery_state'] in {'blocked', 'waiting'}:
            return report
    mapping=raw['instruments']
    current, previous, reference = control.observe_quotes({q['symbol']:mapping[q['symbol']] for q in raw['quotes']},
                                                         runner.policy.max_quote_age_seconds)
    if recover:
        if stamp.tzinfo is None or stamp.astimezone(NY).date() != reference.astimezone(NY).date():
            raise ControlError('交易日已经变化；只核对旧订单，不自动提交上一交易日的剩余委托。')
    else:
        try:
            fresh(stamp,reference,runner.policy.max_quote_age_seconds)
        except ValueError:
            raise ControlError('订单预览已经过期，请重新准备交易取得当前报价。') from None
    a = raw['account']
    account = Account(a['account_id'],a['currency'],amount(a['cash']),
                      tuple((s,amount(q,True)) for s,q in a['positions']),datetime.fromisoformat(a['as_of']))
    quotes = {q['symbol']:Quote(q['symbol'],amount(q['bid'],True),amount(q['ask'],True),datetime.fromisoformat(q['as_of']))
              for q in raw['quotes']}
    snapshot = market_snapshot(stocks,account,mapping,quotes,stamp,loaded.schema,raw['clock_offset_seconds'])
    decision = runner.plan(snapshot)
    orders = tuple(unpack_order(packed(o)) for o in raw['orders'])
    if (decision.decision_id != raw['decision_id']
            or orders != plan_orders(decision,account,quotes,stamp,runner.policy,runner.fees,split_orders=True,tick_size=Decimal('.01'))):
        raise ControlError('订单与已核对策略或仓位限制不一致。')
    # Read fresh quotes again; approved quantities and limit prices never change silently.
    if any(s.price_as_of != previous for s in stocks):
        raise ControlError('行情交易日已经变化，请更新数据并重新准备交易。')
    snapshot = replace(snapshot,clock_offset_seconds=(reference-datetime.now(timezone.utc)).total_seconds())
    with control.session(cfg,snapshot,runner.fees) as (connection,journal):
        if connection.mode != 'broker_paper':
            raise ControlError('模拟订单不得进入实盘或其他账户环境。')
        result = runner.execute(decision,replace(snapshot,quotes=current),connection,journal,
                                approved_orders=orders,expected_account=account,
                                recover=recover,before_submit=before_submit)
    return {'status':'complete','message':f"本轮模拟执行完成：{result['orders']} 笔订单已核对成交。",
            'decision_id':decision.decision_id,
            'targets':[dict(asset_id=t.asset_id,weight=t.weight,score=t.score,**loaded.labels[t.asset_id]) for t in decision.targets],
            'orders':[{'ticker':mapping[o.symbol],'side':o.side,'quantity':str(o.quantity),
                       'limit_price':str(o.limit_price),'client_order_id':o.client_id} for o in orders],
            'recovery_state':'complete',
            'details':{k:v for k,v in result.items() if k != 'account'}}
