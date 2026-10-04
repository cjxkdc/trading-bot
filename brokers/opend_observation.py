"""Read-only observations for a paper order preview; no order session or ledger."""
from datetime import date, datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
import time as monotonic_time

from app.control_contracts import ControlError
from brokers.opend_broker import _account, _code, _orders, STATUSES, TERMINAL, instrument_validator
from framework.trading import Quote, amount, fresh
from platforms.opend import OpenDError, call, connection, records

NY = ZoneInfo('America/New_York')


class ObservationBlocked(OpenDError, ControlError):
    """Safe observation errors survive gateway teardown without becoming login errors."""


def trading_sessions(connector, start, end):
    """Official US calendar only; live market status remains required before orders.

    OpenD excludes holidays/weekends but does not guarantee unscheduled closures.
    https://openapi.moomoo.com/moomoo-api-doc/en/quote/request-trading-days.html
    """
    if (type(start) is not date or type(end) is not date or start > end
            or (end-start).days > 366):
        raise ControlError('交易日历日期范围无效，单次最多查询一年。')
    if connector.mode != 'broker_paper' or connector.environment != 'SIMULATE':
        raise ControlError('自动调度仅允许绑定的模拟账户。')
    with connection(connector._settings()) as (sdk, client, _):
        rows = call(client, 'request_trading_days', market=sdk.TradeDateMarket.US,
                    start=start.isoformat(), end=end.isoformat())
    try:
        if not isinstance(rows, list):
            raise ValueError('not a calendar')
        days = [date.fromisoformat(row['time']) for row in rows]
        if len(set(days)) != len(days) or any(not start <= day <= end for day in days):
            raise ValueError('invalid calendar')
        return sorted(days)
    except (ValueError, TypeError, KeyError):
        raise ObservationBlocked('券商返回的交易日历无效；未使用工作日推测交易日。') from None


def account_observation(connector):
    if connector.mode != 'broker_paper' or connector.environment != 'SIMULATE':
        raise ControlError('准备交易仅允许绑定的模拟账户。')
    with connector._session() as (sdk, client, profile, validate):
        account, _ = _account(sdk, client, profile, lambda: datetime.now(timezone.utc), validate)
        if any(STATUSES.get(r.get('order_status')) not in TERMINAL for r in _orders(sdk, client, profile)):
            raise ObservationBlocked('模拟账户存在未完成委托，请先核对订单。')
        return account


def quote_observation(connector, instruments, max_age, *, now=None):
    supplied_clock = now
    now = now or datetime.now(timezone.utc)
    if connector.mode != 'broker_paper' or connector.environment != 'SIMULATE':
        raise ControlError('准备交易仅允许模拟账户。')
    if not instruments or len(instruments) > 200 or len(set(instruments.values())) != len(instruments):
        raise ControlError('证券映射为空、重复或超出本轮上限。')
    with connection(connector._settings()) as (sdk, client, profile):
        server = call(client,'get_global_state')
        received = monotonic_time.monotonic()
        try:
            reference = datetime.fromtimestamp(float(server['timestamp']),timezone.utc)
            if abs((reference-datetime.now(timezone.utc)).total_seconds()) > 120:
                raise ValueError('clock drift exceeds bound')
        except (ValueError,KeyError,TypeError,OverflowError):
            raise ObservationBlocked('无法校准行情服务器时间，或本机时钟偏差超过两分钟；请同步系统时间。') from None
        if supplied_clock is not None:
            reference = supplied_clock
        codes = [_code(t) for t in instruments.values()]
        states = records(call(client, 'get_market_state', code_list=codes))
        local = now.astimezone(NY)
        if (len(states) != len(codes) or {s.get('code') for s in states} != set(codes)):
            raise ObservationBlocked('券商未返回完整的证券市场状态。')
        if (local.weekday() >= 5 or not time(9,30) <= local.time().replace(tzinfo=None) < time(16)
                or any(s.get('market_state') not in {sdk.MarketState.MORNING, sdk.MarketState.AFTERNOON} for s in states)):
            raise ObservationBlocked('当前不在美股正常交易时段；开盘后重新准备交易。')
        days = call(client, 'request_trading_days', market=sdk.Market.US,
                    start=(local.date()-timedelta(days=15)).isoformat(), end=(local.date()-timedelta(days=1)).isoformat())
        if not isinstance(days, list) or not days:
            raise ObservationBlocked('无法核对最近已完成的美股交易日。')
        previous = max(date.fromisoformat(d['time']) for d in days)
        if not local.date()-timedelta(days=15) <= previous < local.date():
            raise ObservationBlocked('券商交易日历无效。')
        instrument_validator(sdk, client)(tuple(instruments.values()))
        rows = records(call(client, 'get_market_snapshot', code_list=codes))
        if len(rows) != len(codes) or {r.get('code') for r in rows} != set(codes):
            raise ObservationBlocked('没有取得全部目标股票和持仓的行情。')
        lookup = {_code(t): asset for asset,t in instruments.items()}
        quotes = {}
        # Server timestamp is integral seconds. Account for request elapsed time and that precision.
        checked = (reference+timedelta(seconds=monotonic_time.monotonic()-received+1)
                   if supplied_clock is None else supplied_clock)
        # update_time is the vendor's US Eastern timestamp, never the retrieval time.
        for row in rows:
            ticker = row['code'][3:]
            if row.get('suspension') is not False or row.get('sec_status') != sdk.SecurityStatus.NORMAL:
                raise ObservationBlocked(f'{ticker} 当前暂停交易或证券状态异常。')
            try:
                stamp = datetime.fromisoformat(row['update_time']).replace(tzinfo=NY)
                quote = Quote(lookup[row['code']], amount(row['bid_price'], True), amount(row['ask_price'], True), stamp)
                fresh(quote.as_of, checked, max_age)
                if quote.bid > quote.ask or quote.ask / quote.bid > 1.05:
                    raise ValueError('crossed or excessively wide')
            except (ValueError,KeyError,TypeError,ArithmeticError):
                raise ObservationBlocked(f'{ticker} 买卖报价缺失、过期或价差异常；请检查行情权限并重新准备。') from None
            quotes[quote.symbol] = quote
        return quotes, previous, checked
