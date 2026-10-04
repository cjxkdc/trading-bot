"""Bounded read-only probes, deliberately distinct from a strategy universe feed."""
from datetime import datetime, timedelta, timezone
import json
import math
import re
from zoneinfo import ZoneInfo

from platforms.opend import OpenDError, call, configuration_check, connection, settings

SNAPSHOT_FIELDS = ('code', 'update_time', 'last_price', 'bid_price', 'ask_price',
                   'volume', 'turnover', 'suspension', 'equity_valid', 'listing_date')
HISTORY_FIELDS = ('code', 'time_key', 'open', 'high', 'low', 'close', 'volume', 'turnover')


def _table(data, fields):
    if not hasattr(data, 'to_json'):
        raise OpenDError('Expected a market-data table')
    rows = json.loads(data.to_json(orient='records'))
    return [{k: r[k] for k in fields if k in r} for r in rows]


def _probe(fn):
    try:
        data = fn()
        json.dumps(data, allow_nan=False)
        result = {'status': 'available' if data else 'empty', 'data': data}
    except Exception:
        # SDK errors can contain account/device details. Preserve the failed check, not its raw message.
        result = {'status': 'failed', 'message_suppressed': True,
                  'reason': 'Check OpenD login, data permissions and supported API version'}
    result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
    return result


def _history(sdk, client, code, now):
    end = now.astimezone(ZoneInfo('America/New_York')).date() - timedelta(days=1)
    start = end - timedelta(days=600)
    table, next_key = call(client, 'request_history_kline', code=code, start=start.isoformat(),
                           end=end.isoformat(), ktype=sdk.KLType.K_DAY, autype=sdk.AuType.QFQ,
                           max_count=1000, extended_time=False, session=sdk.Session.RTH)
    if next_key:
        raise OpenDError('Daily history response is unexpectedly truncated')
    rows = _table(table, HISTORY_FIELDS)
    dates = set()
    for row in rows:
        day = datetime.fromisoformat(row['time_key']).date()
        if (row['code'] != code or day in dates or not start <= day <= end
                or not math.isfinite(float(row['close'])) or float(row['close']) <= 0):
            raise OpenDError('Invalid or incomplete daily history')
        dates.add(day)
    return sorted(rows, key=lambda r: r['time_key'])


def _financials(client, code):
    value = call(client, 'get_financials_statements', code=code, statement_type=1,
                 financial_type='QUARTERLY_ANNUAL', num=8)
    if not isinstance(value, dict) or not isinstance(value.get('report_list'), list):
        raise OpenDError('Unexpected financial-statement response')
    # These are public statement periods, NOT dates when the information became tradable.
    return {key: value[key] for key in ('structure_list', 'report_list', 'next_key') if key in value}


def collect(sdk, client, provider, symbol, now=None):
    now = now or datetime.now(timezone.utc)
    code = 'US.' + symbol
    checks = {}
    def global_state():
        data = call(client, 'get_global_state')
        return {k: data[k] for k in ('market_us', 'server_ver', 'qot_logined', 'timestamp') if k in data}
    def quota():
        used, remaining, _ = call(client, 'get_history_kl_quota', get_detail=False)
        return {'used': int(used), 'remaining': int(remaining)}
    checks['gateway'] = _probe(global_state)
    checks['history_quota_before'] = _probe(quota)
    checks['snapshot'] = _probe(lambda: _table(call(client, 'get_market_snapshot', code_list=[code]), SNAPSHOT_FIELDS))
    checks['daily_history'] = _probe(lambda: _history(sdk, client, code, now))
    checks['financial_statements'] = _probe(lambda: _financials(client, code))
    fresh_quote = False
    rows = checks['snapshot'].get('data', [])
    try:
        if len(rows) == 1 and rows[0]['code'] == code and rows[0]['suspension'] is False:
            row = rows[0]
            bid, ask = float(row['bid_price']), float(row['ask_price'])
            quoted = datetime.fromisoformat(row['update_time']).replace(tzinfo=ZoneInfo('America/New_York'))
            fresh_quote = math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask and 0 <= (now - quoted).total_seconds() <= 120
    except (KeyError, TypeError, ValueError):
        pass
    blockers = ['Full point-in-time US technology universe is not implemented',
                'TTM operating-income mapping and publication timestamps require validation',
                'QFQ price/dividend treatment must be reconciled with the QuantConnect research feed']
    if not fresh_quote:
        blockers.append('No verified recent bid/ask for the probe symbol; closed-market and delayed quotes cannot execute a current decision')
    bars = len(checks['daily_history'].get('data', []))
    if bars < 253:
        blockers.append('Fewer than 253 validated completed daily bars for the probe symbol')
    return {'provider': provider + '_market', 'status': 'data_probe_completed',
            'mode': 'market_data_diagnostic', 'network_requested': True, 'symbol': symbol,
            'strategy_ready': False, 'universe_complete': False, 'checks': checks,
            'completed_daily_bars': bars, 'recent_bid_ask': fresh_quote, 'blockers': blockers,
            'note': 'Read-only evidence, not a us_technology_daily_v1 snapshot. '
                    'One history symbol may consume OpenD history quota; current-day bars are excluded. '
                    'Financial report period-end is not the publication date.'}


class OpenDMarketData:
    provider = None

    def __init__(self, config_dir=None):
        self.config_dir = config_dir

    def inspect(self, *, connect=False, symbol='AAPL', claim_device=False):
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol) or symbol.startswith('DEMO'):
            raise OpenDError('Use one US stock ticker, for example AAPL')
        if claim_device:
            raise OpenDError('--claim-device is a Tiger-only operation; configure permissions in OpenD')
        check = configuration_check(self.provider, self.config_dir)
        check.update(mode='market_data_diagnostic', strategy_ready=False)
        if not connect:
            return check
        if check['issues']:
            raise OpenDError('Install the selected SDK before connecting')
        with connection(settings(self.provider, self.config_dir)) as (sdk, client, _):
            return collect(sdk, client, self.provider, symbol)
