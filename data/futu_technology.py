"""Read-only current-data collector. Its research schema is never executable.

No strategy or broker imports. Current membership/financials have observed times,
not invented historical publication dates. Raw responses stay in the local cache.
"""
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

from platforms.opend import OpenDError, call, connection, settings
from data.common import DataIssue, utcnow, write_json, finite, collection_lock

SCHEMA = 'futu_technology_preview_v1'
NY = ZoneInfo('America/New_York')
EXCHANGES = {'US_NASDAQ', 'US_NYSE', 'US_AMEX'}
INCOME_FIELDS = {8017, 5034}
INCOME_NAMES = {'营业利润', '營業利潤', '经营利润', '經營溢利', 'Operating Income', 'Operating Profit'}
INTERVALS = {'get_plate_list': 3.2, 'get_plate_stock': 3.2, 'get_stock_basicinfo': 3.2,
             'request_trading_days': 3.2, 'request_history_kline': 0.55,
             'get_financials_statements': 1.1, 'get_market_snapshot': 0.55}








def as_plain(value):
    if hasattr(value, 'to_json'):
        return json.loads(value.to_json(orient='records'))
    return value




def operating_income_ttm(payload, observed_at):
    """Four consecutive single quarters only; annual rows are never added again."""
    periods = {}
    for report in payload.get('report_list', []):
        kind = report.get('financial_type')
        if kind not in {'Q1', 'Q2', 'Q3', 'Q4'}:
            continue
        year = report.get('fiscal_year')
        if type(year) is not int or not 1900 <= year <= observed_at.year + 1:
            raise DataIssue('invalid_fiscal_year')
        index = year * 4 + int(kind[1]) - 1
        if index in periods:
            raise DataIssue('duplicate_financial_quarter')
        periods[index] = report
    if not periods:
        raise DataIssue('no_single_quarter_reports')
    last = max(periods)
    if any(i not in periods for i in range(last - 3, last + 1)):
        raise DataIssue('four_consecutive_quarters_unavailable')
    reports = [periods[i] for i in range(last - 3, last + 1)]
    ends = [date.fromisoformat(r['date_time_str']) for r in reports]
    if (ends[-1] > observed_at.astimezone(NY).date()
            or any(not 60 <= (b - a).days <= 120 for a, b in zip(ends, ends[1:]))
            or (observed_at.astimezone(NY).date() - ends[-1]).days > 200):
        raise DataIssue('invalid_or_stale_financial_periods')
    # A newer annual report with a missing Q4 must not leave us using old TTM.
    if any(date.fromisoformat(r['date_time_str']) > ends[-1] for r in payload.get('report_list', [])):
        raise DataIssue('latest_financial_quarter_unavailable')
    currency = {r.get('currency_code') for r in reports}
    if len(currency) != 1 or not next(iter(currency)):
        raise DataIssue('missing_or_mixed_financial_currency')
    values = []
    for report in reports:
        items = [v for v in report.get('item_list', []) if v.get('field_id') in INCOME_FIELDS]
        if len(items) != 1 or items[0].get('display_name') not in INCOME_NAMES:
            raise DataIssue('operating_income_field_unverified')
        values.append(finite(items[0]['data']))
    return {'value': sum(values), 'currency': next(iter(currency)),
            'quarter_ends': [d.isoformat() for d in ends], 'quarter_values': values,
            'observed_at': observed_at.isoformat(), 'publication_time_verified': False}


def aligned_history(rows, code, sessions, count):
    expected = sessions[-count:]
    if len(expected) != count:
        raise DataIssue('insufficient_reference_sessions')
    by_day = {}
    for row in rows:
        day = date.fromisoformat(row['time_key'][:10])
        if row['code'] != code or day in by_day or day > sessions[-1]:
            raise DataIssue('invalid_daily_bar_identity_or_date')
        by_day[day] = row
    if any(day not in by_day for day in expected):
        raise DataIssue('incomplete_daily_history')
    prices = [finite(by_day[day]['close']) for day in expected]
    turnovers = [finite(by_day[day]['turnover']) for day in expected[-20:]]
    if min(prices) <= 0 or min(turnovers) <= 0:
        raise DataIssue('nonpositive_price_or_turnover')
    return prices, turnovers




class CachedQuoteClient:
    """Successful responses only; same New York day, exact request, atomic writes."""
    def __init__(self, client, directory, *, clock=utcnow, monotonic=time.monotonic, sleep=time.sleep):
        self.client, self.directory = client, Path(directory)
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.last = {}
        self.hits, self.requests = 0, 0

    def request(self, method, *, use_cache=True, **kwargs):
        if method not in INTERVALS and method != 'get_history_kl_quota':
            raise DataIssue('unsupported_quote_operation')
        key = {'method': method, 'arguments': kwargs}
        digest = sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        now = self.clock()
        path = self.directory / now.astimezone(NY).date().isoformat() / (digest + '.json')
        if use_cache and path.is_file():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                stamp = datetime.fromisoformat(saved['observed_at'])
                if (saved['request'] == key and stamp.tzinfo is not None and stamp <= now
                        and stamp.astimezone(NY).date() == now.astimezone(NY).date()):
                    self.hits += 1
                    return saved
            except (ValueError, KeyError, TypeError):
                pass
        delay = self.last.get(method, -1e10) + INTERVALS.get(method, 0.55) - self.monotonic()
        if delay > 0:
            self.sleep(delay)
        self.last[method] = self.monotonic()
        value = as_plain(call(self.client, method, **kwargs))
        # call() unwraps multiple results as a tuple; next-page and quota stay explicit.
        if isinstance(value, tuple):
            value = [as_plain(item) for item in value]
        saved = {'request': key, 'observed_at': self.clock().isoformat(), 'data': value}
        if use_cache:
            write_json(path, saved)
        self.requests += 1
        return saved


class FutuTechnologyData:
    def __init__(self, config_dir, source_config, cache_dir):
        self.config_dir, self.source_config = Path(config_dir), Path(source_config)
        self.cache_dir = Path(cache_dir)

    def collect(self, out, *, price_bars=253, progress=None):
        with collection_lock(self.cache_dir):
            return self._collect_locked(out, price_bars=price_bars, progress=progress)

    def _collect_locked(self, out, *, price_bars, progress):
        cfg = json.loads(self.source_config.read_text(encoding='utf-8-sig'))
        if (cfg.get('schema_version') != 1 or not isinstance(cfg.get('industries'), dict)
                or not cfg['industries'] or not 1 <= cfg.get('max_symbols', 0) <= 1000
                or any(not re.fullmatch(r'US\.LIST\d+', code) for code in cfg['industries'])
                or type(price_bars) is not int or not 21 <= price_bars <= 700):
            raise DataIssue('unsupported_technology_source_configuration')
        out = Path(out)
        out.mkdir(parents=True, exist_ok=False)
        def notify(message):
            write_json(out / 'progress.json', {'message': message, 'updated_at': utcnow().isoformat()})
            if progress:
                progress(message)
        notify('正在读取 Futu 科技行业股票池…')
        # Quote context only. No account/order/trade-unlock methods are available here.
        data_error = None
        with connection(settings('futu', self.config_dir)) as (sdk, client, _):
            api = CachedQuoteClient(client, self.cache_dir)
            # Let the connection close normally; preserve data failures instead of
            # having the gateway context label them as a login/connection failure.
            try:
                result = self._collect(api, sdk, cfg, price_bars, notify)
            except (DataIssue, ValueError, KeyError, TypeError) as exc:
                data_error = exc if isinstance(exc, DataIssue) else DataIssue('invalid_provider_data_structure')
        if data_error:
            notify('数据采集未完成：' + str(data_error))
            raise data_error
        result.update(schema=SCHEMA, execution_allowed=False, synthetic=False,
                      finished_at=utcnow().isoformat(), source_configuration=cfg,
                      cache_hits=api.hits, network_requests=api.requests)
        write_json(out / 'preview-data.json', result)
        notify(f"采集结束：有效数据 {len(result['stocks'])} / 候选股票 {result['candidate_count']}；仅供预览。")
        return out / 'preview-data.json'

    def _collect(self, api, sdk, cfg, count, notify):
        started = utcnow()
        today = started.astimezone(NY).date()
        end, start = today - timedelta(days=1), today - timedelta(days=max(600, count * 2))
        calendar = api.request('request_trading_days', market=sdk.TradeDateMarket.US,
                               start=start.isoformat(), end=end.isoformat())['data']
        sessions = sorted(date.fromisoformat(row['time']) for row in calendar)
        if not sessions or len(sessions) != len(set(sessions)) or sessions[-1] > end or len(sessions) < count:
            raise DataIssue('invalid_reference_calendar')
        plates = api.request('get_plate_list', market=sdk.Market.US, plate_class=sdk.Plate.INDUSTRY)['data']
        available = {r['code']: r['plate_name'] for r in plates}
        if not set(cfg['industries']).issubset(available):
            raise DataIssue('configured_industry_missing_from_provider')
        members = {}
        for i, code in enumerate(cfg['industries'], 1):
            rows = api.request('get_plate_stock', plate_code=code)['data']
            if not rows:
                raise DataIssue('industry_membership_is_empty')
            for row in rows:
                if row.get('stock_type') != 'STOCK':
                    continue
                member = dict(row, industry=code, industry_name=available[code])
                if row['code'] in members and members[row['code']]['industry'] != code:
                    raise DataIssue('ambiguous_industry_membership')
                members[row['code']] = member
            notify(f"股票池：{i}/{len(cfg['industries'])} 个行业，{len(members)} 只股票")
        if not members or len(members) > cfg['max_symbols']:
            raise DataIssue('universe_empty_or_larger_than_configured_quota_budget')
        excluded, candidates = [], []
        codes = sorted(members)
        for i in range(0, len(codes), 200):
            batch = codes[i:i + 200]
            rows = api.request('get_stock_basicinfo', market=sdk.Market.US,
                               stock_type=sdk.SecurityType.STOCK, code_list=batch)['data']
            basic = {r['code']: r for r in rows}
            if set(basic) != set(batch) or len(basic) != len(rows):
                raise DataIssue('incomplete_security_metadata')
            for code in batch:
                row = basic[code]
                reason = None
                if (row.get('stock_type') != 'STOCK' or row.get('delisting') is not False
                        or row.get('exchange_type') not in EXCHANGES):
                    reason = 'outside_supported_active_US_exchange_stocks'
                elif date.fromisoformat(row['listing_date']) > sessions[-count]:
                    reason = 'listing_too_recent_for_history_requirement'
                if reason:
                    excluded.append({'code': code, 'reason': reason})
                else:
                    if not re.fullmatch(r'US\.[A-Z][A-Z0-9.\-]{0,14}', code) or int(row['stock_id']) <= 0:
                        raise DataIssue('invalid_security_identifier')
                    candidates.append(dict(members[code], **{k: row[k] for k in ('stock_id', 'listing_date', 'exchange_type')}))
        quota = api.request('get_history_kl_quota', use_cache=False, get_detail=True)['data']
        if not isinstance(quota, list) or len(quota) != 3:
            raise DataIssue('history_quota_unavailable')
        used_codes = {r['code'] for r in (quota[2] or [])}
        needed = len({r['code'] for r in candidates} - used_codes)
        if needed > int(quota[1]):
            raise DataIssue('insufficient_history_symbol_quota_for_complete_universe')
        stocks, issues = [], []
        for i, member in enumerate(candidates, 1):
            code = member['code']
            stage = 'daily_history'
            try:
                history = api.request('request_history_kline', code=code, start=start.isoformat(),
                                      end=sessions[-1].isoformat(), ktype=sdk.KLType.K_DAY,
                                      autype=sdk.AuType.QFQ, max_count=1000,
                                      extended_time=False, session=sdk.Session.RTH)
                rows, next_key = history['data']
                if next_key is not None:
                    raise DataIssue('history_response_truncated')
                prices, volumes = aligned_history(rows, code, sessions, count)
                stage = 'financial_statements'
                facts = api.request('get_financials_statements', code=code, statement_type=1,
                                    financial_type='QUARTERLY_ANNUAL', num=12)
                income = operating_income_ttm(facts['data'], datetime.fromisoformat(facts['observed_at']))
                stocks.append({'asset_id': 'FUTU:' + str(member['stock_id']), 'ticker': code[3:],
                               'name': member['stock_name'], 'industry': member['industry'],
                               'industry_name': member['industry_name'], 'exchange': member['exchange_type'],
                               'observed_at': income['observed_at'], 'price_as_of': sessions[-1].isoformat(),
                               'operating_income_ttm': income['value'], 'financial_provenance': income,
                               'adjusted_closes': prices, 'dollar_volumes': volumes})
            except (DataIssue, ValueError, KeyError, TypeError, OpenDError) as exc:
                reason = str(exc) if isinstance(exc, DataIssue) else 'request_failed_or_invalid_response'
                issues.append({'code': code, 'stage': stage, 'reason': reason})
            if i == 1 or i % 5 == 0 or i == len(candidates):
                notify(f"采集 {i}/{len(candidates)}：{code}；有效 {len(stocks)}，缺失 {len(issues)}；缓存命中 {api.hits}")
        notify('正在读取市场快照…')
        quotes = []
        valid_codes = ['US.' + s['ticker'] for s in stocks]
        for i in range(0, len(valid_codes), 200):
            try:
                result = api.request('get_market_snapshot', use_cache=False, code_list=valid_codes[i:i + 200])
                for row in result['data']:
                    quotes.append({k: row[k] for k in ('code', 'update_time', 'last_price', 'bid_price', 'ask_price', 'suspension') if k in row})
            except OpenDError:
                issues.append({'stage': 'market_snapshot', 'reason': 'snapshot_request_failed'})
        return {'started_at': started.isoformat(), 'price_as_of': sessions[-1].isoformat(),
                'provider': 'futu', 'universe_id': cfg['universe_id'],
                'universe_observed_at': started.isoformat(), 'candidate_count': len(members),
                'history_candidate_count': len(candidates), 'stocks': stocks, 'quotes': quotes,
                'excluded': excluded, 'issues': issues, 'collection_complete': not issues,
                'limitations': [
                    'Futu 当前行业股票池；ADR 和主上市身份尚未核实，与 QC 股票池口径未对齐。',
                    '财报保留实际采集时间，未取得公告时间，不能回填历史或用于下单。',
                    'Futu 前复权价格及成交额口径尚未与 QC 核对。',
                    '有缺失数据时，排名只代表本次有效数据范围，不代表完整股票池。']}
