"""Provider-neutral reconciliation and exact frozen-batch continuation."""
from app.control_contracts import ControlError
from framework.execution import Executor


def reconcile_orders(*, control, policy, fees):
    """Read broker status after a restart or the close; never submit/cancel/reprice."""
    if control.mode not in {'broker_paper','live'} or not callable(getattr(control, 'reconciliation_session', None)):
        raise ControlError('当前券商没有声明订单核对能力。')
    if not control.state_dir.exists():
        return {'status': 'complete', 'recovery_state': 'complete',
                'message': '尚未创建执行账本，没有本程序的订单需要核对。',
                'details': {'submitted': 0, 'orders': [], 'batches': []}}
    with control.reconciliation_session(fees) as (broker, journal):
        report = Executor(broker, journal, policy, fees).reconcile()
    state = report['recovery_state']
    messages = {
        'complete': '订单记录已核对完成，没有未完成的本程序委托。',
        'ready': '已提交订单已成交，剩余委托仍保留原数量和限价，等待继续执行。',
        'waiting': '委托已受理或部分成交，正在等待券商确认；未重复下单。',
        'blocked': '存在状态不明、撤单或拒单，自动执行已暂停，请核对订单。',
    }
    statuses = {item['status'] for item in report['orders']}
    if state == 'blocked' and not statuses - {'planned', 'cancelled', 'rejected'}:
        messages['blocked'] = (f"有 {report['terminal']} 笔委托已被券商撤销或拒绝，自动执行已暂停。"
                               '在券商端核对后，可用「关闭已撤单/拒单」结束本批次；不会补单或撤单。')
    if state == 'complete' and report.get('abandoned'):
        messages['complete'] = f"账本已核对，无待处理委托；历史上有 {report['abandoned']} 笔未提交计划已明确放弃，不代表组合达到目标仓位。"
    return {'status': 'paused' if state in {'waiting', 'blocked'} else state,
            'recovery_state': state, 'message': messages[state], 'details': report}


def close_unsubmitted(*, control, policy, fees):
    """Explicit user-confirmed closure; authorization is enforced by the Portal caller."""
    if control.mode not in {'broker_paper','live'} or not callable(getattr(control, 'reconciliation_session', None)):
        raise ControlError('当前券商没有声明订单核对能力。')
    if not control.state_dir.exists():
        raise ControlError('没有执行账本，无法关闭未提交的订单计划。')
    with control.reconciliation_session(fees) as (broker, journal):
        report = Executor(broker, journal, policy, fees).close_unsubmitted()
    count = report['newly_abandoned']
    return {'status':'complete', 'recovery_state':'complete', 'details':report,
            'message': f'已明确放弃 {count} 笔从未提交的订单计划，并保留审计记录；未撤单、未补单，不代表达到目标仓位。'}


def close_terminal(*, control, policy, fees):
    """Explicit user-confirmed closure of broker-confirmed cancelled/rejected orders."""
    if control.mode not in {'broker_paper','live'} or not callable(getattr(control, 'reconciliation_session', None)):
        raise ControlError('当前券商没有声明订单核对能力。')
    if not control.state_dir.exists():
        raise ControlError('没有执行账本，无需关闭委托。')
    with control.reconciliation_session(fees) as (broker, journal):
        report = Executor(broker, journal, policy, fees).close_terminal()
    closed, abandoned = report['newly_closed'], report['newly_abandoned']
    tail = f'，并放弃 {abandoned} 笔从未提交的计划' if abandoned else ''
    return {'status': 'complete', 'recovery_state': report['recovery_state'], 'details': report,
            'message': f'已关闭 {closed} 笔券商确认撤销或拒绝的委托{tail}，审计记录已保存；'
                       '已成交部分按实际持仓保留，未补单、未撤单，不代表达到目标仓位。'}


def recover_prepared(*, root, raw, context, token, source, runner, control, cfg, clock,
                     before_submit=None):
    """Continue only a same-session, already claimed batch with validated holdings.

    Unlike a fresh preview, elapsed time alone does not discard durable intent.
    Quotes are refreshed, quantities/limits remain frozen, account changes must
    match broker fills, and transmission still observes every execution guard.
    """
    from app.paper_preparation import _execute_prepared
    return _execute_prepared(root=root, raw=raw, context=context, token=token, source=source,
                             runner=runner, control=control, cfg=cfg, clock=clock,
                             before_submit=before_submit, recover=True)
