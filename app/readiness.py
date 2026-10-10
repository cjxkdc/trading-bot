"""Authoritative, side-effect-free operation eligibility and human-readable blockers."""
import math
import time
from app.operations import WORKBENCH_ACTIONS, BROKER_CHECKS, DATA_CAPABILITIES


def operation_states(*, broker, source, snapshot, preview, verified, account, preview_authorized,
                     busy=False, closing=False, compatibility_issue=None, context_issue=None,
                     strategy_issue=None, authorization=None, clock=None):
    stamp = time.time() if clock is None else clock
    result = {}
    for action in sorted(WORKBENCH_ACTIONS):
        blockers = []
        def block(code, message, next_action=None):
            blockers.append({'code': code, 'message': message, 'next_action': next_action})
        if closing:
            block('stopping', '后台正在停止，请等待完成。')
        elif busy:
            block('busy', '当前任务尚未完成，请等待或查看运行记录。')
        if action == 'preflight':
            # Missing prerequisites are the purpose of this diagnostic job.
            result[action] = {'enabled': not blockers, 'blockers': blockers,
                              'message': blockers[0]['message'] if blockers else '检查账户、数据、选股与订单；不会下单。',
                              'next_action': None}
            continue
        needs_broker = action in BROKER_CHECKS or action in {'prepare', 'execute'}
        needs_data = action not in BROKER_CHECKS
        if needs_broker:
            if not broker.get('enabled'):
                block('broker_unavailable', broker.get('reason') or '券商模块不可用。', 'broker')
            capability = ('reconcile' if action in {'resolve-remainder', 'close-terminal'} else action if action in {'calendar', 'reconcile', 'observe'}
                          else 'inspect' if action in BROKER_CHECKS else action)
            if capability not in broker.get('capabilities', []):
                block('broker_capability', '当前券商不支持此操作。', 'broker')
            if (action == 'status' and broker.get('authentication', {}).get('mode') == 'oauth'
                    and not (authorization or {}).get('selected')):
                block('authorization_required', '请先完成官网授权并选择账户。', 'broker')
        if needs_data:
            if not source.get('enabled'):
                block('source_unavailable', source.get('reason') or '数据模块不可用。', 'data')
            required = DATA_CAPABILITIES.get(action)
            if required and required not in source.get('capabilities', []):
                block('source_capability', '当前数据模块不支持此操作。', 'data')
            if action != 'configure-data':
                if strategy_issue:
                    block('strategy_unavailable', strategy_issue, 'strategy')
                if compatibility_issue:
                    block('incompatible', compatibility_issue, 'data')
                if context_issue:
                    block('configuration', context_issue, 'data')
            if action in {'refresh', 'data-status', 'backfill'} and source.get('configured') is False:
                block('source_unconfigured', '请先配置当前数据源。', 'data')
        if action in {'prepare', 'execute'} and broker.get('execution_enabled', True) is not True:
            block('execution_permission', broker.get('execution_reason') or '此账户尚未获得执行权限。', 'broker')
        if action in {'prepare', 'execute'} and broker.get('mode') != 'paper' and not verified:
            block('account_unverified', '账户尚未核验或核验已过期，请重新检查连接。', 'status')
        if action in {'preview', 'prepare'} and not snapshot:
            block('snapshot_missing', '请先更新或导入数据。', 'refresh')
        if (action == 'prepare' and snapshot and snapshot.get('research_only')
                and not {'paper_signal','execution_signal'}.intersection(source.get('capabilities', []))):
            block('signal_unsupported', '当前数据模块只支持研究预览，尚未提供交易信号。', 'data')
        if action == 'execute':
            if not preview:
                block('preview_missing', '请先预览选股并准备订单。', 'preview')
            elif preview.get('no_orders') is True:
                block('no_orders', '本轮无需提交订单：持仓已匹配目标，或差额不足最小交易单位。', None)
            elif not preview.get('execution_ready') or not preview_authorized:
                why = preview.get('blockers') or []
                block('orders_not_ready', str(why[0]) if why else '请先准备订单，核对持仓、报价和买卖数量。', 'prepare')
            else:
                expiry = preview.get('expires_at')
                if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry) or stamp >= expiry):
                    block('quotes_expired', '订单报价已过期，请重新准备订单。', 'prepare')
            if broker.get('mode') != 'paper' and (account or {}).get('status') != 'connected':
                block('account_unknown', '请先检查当前账户连接。', 'status')
        if action == 'recover':
            block('internal_recovery', '订单续行由自动运行服务核对冻结批次后处理。')
        if action == 'resolve-remainder' and broker.get('mode') not in {'broker_paper','live'}:
            block('remote_resolution_only', '只能结束联网账户中尚未提交的余单。')
        if action == 'close-terminal' and broker.get('mode') not in {'broker_paper','live'}:
            block('remote_resolution_only', '只能关闭联网账户中已撤单或拒单的委托。')
        if action == 'close-untransmitted' and broker.get('mode') not in {'broker_paper','live'}:
            block('remote_resolution_only', '只能关闭联网账户中从未送达券商的委托。')
        result[action] = {'enabled': not blockers, 'blockers': blockers,
                          'message': blockers[0]['message'] if blockers else '可以执行此操作。',
                          'next_action': blockers[0]['next_action'] if blockers else None}
    return result
