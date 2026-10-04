"""Bounded Tiger data diagnostics. Output is evidence, not a trading snapshot."""
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from platforms.tiger import GATEWAYS, configuration_check

READ_METHODS = frozenset({
    'get_quote_permission', 'kline_quota', 'quote_real_time', 'quote_delay',
    'kline', 'financial_report', 'stock_industry',
})


class TigerMarketError(RuntimeError):
    """Sanitized error; never expose signed requests or SDK exception text."""


def _client_class():
    from tigeropen.quote.quote_client import QuoteClient

    class MarketClient(QuoteClient):
        def execute(self, request, url=None):
            method = getattr(request, '_method', None)
            if method == 'grab_quote_permission':
                if not getattr(self, '_claim_authorized', False):
                    raise TigerMarketError('Device claim requires explicit --claim-device')
                self._claim_authorized = False  # At most one attempt, never an implicit retry.
            elif method not in READ_METHODS:
                raise TigerMarketError('Request outside market-data diagnostics blocked')
            if url is not None and url not in GATEWAYS:
                raise TigerMarketError('Unexpected Tiger gateway blocked')
            return super().execute(request, url=url)
    return MarketClient


def _connect(config_dir, *, claim_device=False):
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    config = TigerOpenClientConfig(props_path=str(Path(config_dir).resolve()),
                                   sandbox_debug=False, enable_dynamic_domain=False)
    if (config.server_url not in GATEWAYS or config.quote_server_url not in GATEWAYS
            or not config.account or not config.private_key or not config.tiger_id):
        raise TigerMarketError('Invalid Tiger market-data configuration')
    config.timeout, config.retry_max_tries, config.retry_max_time = 10, 1, 10
    config.token_refresh_duration, config.log_path = 0, None
    logger = logging.Logger('trading_bot_tiger_market')
    logger.addHandler(logging.NullHandler())
    logger.propagate, logger.disabled = False, True
    # The SDK defaults to claiming device access. Diagnostics must opt OUT.
    client = _client_class()(config, logger=logger, is_grab_permission=False)
    client._claim_authorized = claim_device
    return client


def _rows(value, fields):
    if value is None:
        return []
    if hasattr(value, 'to_json'):
        value = json.loads(value.to_json(orient='records'))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise TigerMarketError('Unexpected market-data response shape')
    # Retain only public data fields, never raw SDK objects or account metadata.
    return [{key: row[key] for key in fields if key in row} for row in value]


def _probe(fn, fields):
    try:
        data = _rows(fn(), fields)
        result = {'status': 'available' if data else 'empty', 'data': data}
    except Exception as exc:
        message = str(getattr(exc, 'msg', '')).lower()
        code = str(getattr(exc, 'code', ''))
        result = {'status': 'failed', 'code': code if code.isdigit() else None,
                  'permission_error': any(t in message for t in ('permission', '权限')),
                  'device_error': any(t in message for t in ('device', '设备')),
                  'message_suppressed': True}
    result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
    return result


def _completed_bars(check, symbol, now):
    """Count only valid earlier-day bars; retain raw response separately for audit."""
    sessions = set()
    try:
        for row in check.get('data', []):
            if row['symbol'] != symbol or not math.isfinite(float(row['close'])) or float(row['close']) <= 0:
                return 0
            session = datetime.fromtimestamp(float(row['time']) / 1000, timezone.utc).astimezone(
                ZoneInfo('America/New_York')).date()
            if session >= now.astimezone(ZoneInfo('America/New_York')).date():
                continue
            if session in sessions:
                return 0
            sessions.add(session)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return 0
    return len(sessions)


def _quote_usable(check, symbol, now):
    rows = check.get('data', [])
    if len(rows) != 1:
        return False
    try:
        row = rows[0]
        bid, ask = float(row['bid_price']), float(row['ask_price'])
        age = now.timestamp() - float(row['latest_time']) / 1000
        return (row['symbol'] == symbol and row.get('status') == 'NORMAL'
                and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
                and 0 <= age <= 120)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def collect(client, symbol, *, claim_device=False):
    from tigeropen.common.consts import BarPeriod, FinancialReportPeriodType, Market, QuoteRight
    checks = {}
    report = {'schema': 'tiger_market_diagnostic_v1', 'source': 'Tiger OpenAPI', 'symbol': symbol,
              'started_at': datetime.now(timezone.utc).isoformat(), 'network_requested': True,
              'orders_submitted': False, 'universe_complete': False, 'strategy_ready': False,
              'device_claim_requested': claim_device, 'checks': checks}
    if claim_device:
        checks['device_claim'] = _probe(client.grab_quote_permission, ('name', 'expire_at'))
    checks['permissions'] = _probe(client.get_quote_permission, ('name', 'expire_at'))
    checks['history_quota'] = _probe(client.get_kline_quota, ('method', 'remain', 'used'))
    checks['realtime'] = _probe(lambda: client.get_stock_briefs([symbol]),
        ('symbol', 'bid_price', 'ask_price', 'bid_size', 'ask_size', 'latest_price', 'latest_time', 'status'))
    checks['delayed'] = _probe(lambda: client.get_stock_delay_briefs([symbol]),
        ('symbol', 'time', 'open', 'high', 'low', 'close', 'volume'))
    bar_fields = ('symbol', 'time', 'open', 'high', 'low', 'close', 'volume', 'amount')
    # One explicitly selected symbol only. Do not spend the quota on a whole sector.
    for name, right in (('daily_adjusted', QuoteRight.BR), ('daily_raw', QuoteRight.NR)):
        checks[name] = _probe(lambda right=right: client.get_bars(
            [symbol], period=BarPeriod.DAY, right=right, limit=260), bar_fields)
    checks['industry'] = _probe(lambda: client.get_stock_industry(symbol, Market.US),
        ('industry_level', 'id', 'name_en'))
    checks['operating_income_ttm'] = _probe(lambda: client.get_financial_report(
        [symbol], Market.US, ['operating_income'], FinancialReportPeriodType.LTM),
        ('symbol', 'currency', 'field', 'value', 'period_end_date', 'filing_date'))
    now = datetime.now(timezone.utc)
    report['finished_at'] = now.isoformat()
    report['completed_adjusted_bar_count'] = _completed_bars(checks['daily_adjusted'], symbol, now)
    report['recent_two_sided_quote'] = _quote_usable(checks['realtime'], symbol, now)
    blockers = []
    if not report['recent_two_sided_quote']:
        blockers.append('No recent normal-session bid/ask quote for the test symbol; delayed data is not a substitute')
    if checks['operating_income_ttm']['status'] != 'available':
        blockers.append('TTM operating-income data is unavailable; the profitability filter remains enabled')
    if report['completed_adjusted_bar_count'] < 253:
        blockers.append('Fewer than 253 valid completed adjusted daily bars for the test symbol')
    blockers.extend([
        'Full eligible technology universe and stable security identifiers have not been collected or verified',
        'Point-in-time fundamentals, trading calendar and adjustment compatibility with the QC baseline require validation',
    ])
    report.update(status='data_probe_completed', blockers=blockers,
                  note='Diagnostic only. No strategy targets or executable snapshot are produced. '
                       'Quote latest_time is a last-trade time, not proof of bid/ask update time. '
                       'Current-day bars are excluded from the completed count. '
                       'Industry classifications may differ from Morningstar in QuantConnect.')
    return report


class TigerMarketData:
    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)

    def inspect(self, *, connect=False, symbol='AAPL', claim_device=False):
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol):
            raise TigerMarketError('Use one US stock ticker, for example AAPL')
        if claim_device and not connect:
            raise TigerMarketError('--claim-device requires --connect')
        check = configuration_check(self.config_dir)
        check.update(mode='market_data_diagnostic', strategy_ready=False)
        if not connect:
            return check
        if check['issues']:
            raise TigerMarketError('Tiger configuration is not ready; run market-status without --connect')
        try:
            return collect(_connect(self.config_dir, claim_device=claim_device), symbol,
                           claim_device=claim_device)
        except TigerMarketError:
            raise
        except Exception:
            raise TigerMarketError('Tiger market-data connection failed; check authorization and connectivity') from None
