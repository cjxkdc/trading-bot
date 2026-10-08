"""Credential-free failure evidence. Never serialize exception text or locals."""
from pathlib import Path
import re
from uuid import uuid4

from framework.contracts import SubmissionRejected
from platforms.opend import OpenDError

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('error_code', 'phase', 'exception_type', 'diagnostic_id', 'code_locations')
ORDER_FIELDS = {'code', 'trd_side', 'order_type', 'qty', 'price', 'remark', 'trd_env',
                'currency', 'time_in_force', 'fill_outside_rth', 'session'}
SOURCE_DIRS = {'app', 'framework', 'brokers', 'platforms', 'data', 'strategies'}
PHASES = {'offline', 'status', 'observe', 'preview', 'prepare', 'execute', 'refresh', 'data-status',
          'configure-data', 'calendar', 'reconcile', 'recover', 'resolve-remainder', 'close-terminal', 'automation',
          'strategy-check', 'strategy-preview', 'strategy-refresh', 'strategy-cycle',
          'strategy-reconcile', 'strategy-resolve', 'strategy-close-terminal', 'worker-transport', 'preflight'}

# These keys are exact literals from our own adapters, never vendor response text.
OPEND_REASONS = {
    'Missing ticker mapping for asset ID': ('instrument_mapping_missing', '订单缺少历史股票代码映射；请核对冻结订单与证券标识。'),
    'Ambiguous asset ID to ticker mapping': ('instrument_mapping_ambiguous', '证券标识与股票代码映射重复；请核对冻结订单。'),
    'Snapshot must map every held stock to a stable asset ID': ('holding_mapping_missing', '订单快照未覆盖全部现有持仓；请核对持仓与冻结快照。'),
    'OpenD order identity differs from the saved intent': ('broker_order_identity_mismatch', '券商订单回报与冻结委托的字段不一致；请按订单标识核对券商记录，暂勿重发。'),
    'Inconsistent OpenD order identifier, fill or terminal state': ('broker_order_state_inconsistent', '券商订单标识或成交状态未通过一致性检查；请核对订单及成交记录。'),
    'Invalid or mismatched ledger; do not delete it to retry': ('ledger_binding_or_integrity', '订单账本绑定或完整性未通过检查；请保留账本并核对部署与账户绑定。'),
    'Order ledger is missing; never recreate lost order state': ('ledger_missing', '券商订单账本缺失；请恢复原账本后核对，不能重建空账本重试。'),
    'OpenD is logged in as a different platform user': ('gateway_login_mismatch', 'OpenD 登录身份与绑定不一致；请核对所选模拟账户连接。'),
    'Unsupported OpenD SDK version': ('gateway_sdk_version', 'OpenD SDK 版本与应用要求不一致；请核对部署依赖版本。'),
    'Unable to load the pinned OpenD SDK; check its dependencies': ('gateway_sdk_unavailable', 'OpenD SDK 无法加载；请核对服务器依赖和版本。'),
    'OpenD connection failed; start and log in to the selected local gateway': ('gateway_connection_failed', 'OpenD 连接未建立；请核对网关运行和登录状态。'),
    'Only whole-share US limit orders up to USD 100000 and three price decimals are supported': ('order_constraints_rejected', '委托数量、金额或价格精度不符合券商适配器限制；该委托尚未发送。'),
    'Only US stock tickers are supported; synthetic symbols are excluded': ('instrument_code_rejected', '证券代码不符合美股下单格式；请核对历史代码映射。'),
    'Insufficient USD cash including estimated fees': ('order_cash_insufficient', '美元可用现金不足以覆盖委托和预估费用；请重新核对账户与订单。'),
    'Cannot sell unowned or unavailable shares': ('order_shares_unavailable', '卖出数量超过当前可卖持仓；请核对已成交和冻结股数。'),
    'Account has unresolved orders': ('broker_orders_unresolved', '账户仍有未核对委托；请先核对已有订单。'),
    'Client ID was reused for a different order': ('order_intent_conflict', '同一订单标识对应不同委托内容；请保留原批次并核对，暂勿重发。'),
    'Order claim failed or already exists; reconcile before retry': ('transmission_claim_uncertain', '发送登记未确认或已存在；请核对原订单，暂勿重发。'),
    'Only listed US stocks and ETFs are supported; derivatives are excluded': ('instrument_type_rejected', '券商证券资料不符合已上市美股或 ETF 要求；请核对证券资料。'),
    'Incomplete broker instrument metadata': ('instrument_metadata_incomplete', '券商证券资料不完整；请核对网关行情权限和证券资料。'),
    'Account must contain only long USD stock positions': ('account_position_type', '账户持仓类型或币种未通过检查；请核对模拟账户持仓。'),
    'Missing or invalid OpenD cash, position or order amount': ('broker_amount_invalid', '券商资金、持仓或订单金额字段缺失或无效；请核对回报字段。'),
}
for _method in ('accinfo_query', 'position_list_query', 'order_list_query', 'history_order_list_query',
                'get_acc_list', 'get_stock_basicinfo'):
    for _suffix in ('; check login, permissions and supported account type',
                    '; reconcile any pending transmission before retry'):
        OPEND_REASONS['OpenD request failed: ' + _method + _suffix] = (
            'gateway_' + _method + '_failed', 'OpenD 只读查询未取得可靠结果；请核对网关登录、权限和对应账户。')


def _chain(exc):
    seen = set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(seen) < 5:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__


def _exception_type(exc):
    kind = type(exc)
    if kind.__module__ == 'builtins':
        return kind.__name__ if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,70}', kind.__name__) else 'Error'
    if isinstance(exc, SubmissionRejected):
        return 'SubmissionRejected'
    if isinstance(exc, OpenDError):
        return 'OpenDError'
    allowed = {'app.control_contracts': 'ControlError', 'data.common': 'DataIssue',
               'framework.execution': 'ExecutionBlocked', 'brokers.tiger_paper': 'TigerPaperError'}
    return allowed.get(kind.__module__, 'ExternalError')


def _locations(exc, root):
    locations = []
    for cause in reversed(list(_chain(exc))):
        trace = cause.__traceback__
        while trace:
            code = trace.tb_frame.f_code
            try:
                relative = Path(code.co_filename).resolve().relative_to(root)
                if (relative.parts[0] in SOURCE_DIRS and relative.suffix == '.py'
                        and all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in relative.parts)):
                    item = dict(file=relative.as_posix(), line=trace.tb_lineno,
                                function=code.co_name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,80}', code.co_name) else '<call>')
                    if item not in locations:
                        locations.append(item)
            except (OSError, ValueError):
                pass
            trace = trace.tb_next
    return locations[-8:]


def failure_details(exc, phase, *, root=ROOT, fallback='模块操作未通过检查，请核对当前模块的配置、数据格式和账户绑定。'):
    """Return allowlisted context only; the exception's arbitrary text stays private."""
    code, message = 'module_operation_failed', fallback
    if isinstance(exc, SubmissionRejected):
        code, message = 'broker_pre_submit_rejected', '委托在发送前被本地检查拒绝，已保留拒绝记录；当前批次仍暂停，请人工核对处理。'
    for cause in _chain(exc):
        if isinstance(cause, OpenDError) and len(cause.args) == 1 and type(cause.args[0]) is str:
            known = OPEND_REASONS.get(cause.args[0])
            if known:
                code, message = known
                break
    details = dict(error_code=code, phase=phase if phase in PHASES else 'module',
                   exception_type=_exception_type(exc), diagnostic_id=uuid4().hex,
                   code_locations=_locations(exc, Path(root).resolve()), message=message)
    if isinstance(exc, SubmissionRejected):
        details['submission_state'] = 'rejected_before_transmission'
    from brokers.opend_broker import OpenDOrderIdentityError
    for cause in _chain(exc):
        if type(cause) is OpenDOrderIdentityError:
            details['mismatched_fields'] = sorted({field for field in cause.mismatched_fields
                                                   if type(field) is str and field in ORDER_FIELDS})
            break
    return details


def diagnostic_fields(result):
    """Copy only fields generated by failure_details across orchestration layers."""
    return {key: result[key] for key in (*FIELDS, 'submission_state', 'mismatched_fields') if key in result}
