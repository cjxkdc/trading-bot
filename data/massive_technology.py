"""Current US technology research data from Massive, independent of the broker.

This is a current SIC universe, not a historical Morningstar universe. Explicit
observation dates and a non-executable schema prevent use as a PIT backtest or
as a stale execution quote. Prices are split-adjusted, not dividend-adjusted.
"""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from data.common import DataIssue, collection_lock, finite, utcnow, write_json
from data.massive import INCOME, TICKERS, TICKER, MassiveClient, MassiveError, settings

SCHEMA = 'current_equity_research_v1'
NY = ZoneInfo('America/New_York')
EXCHANGES = {'XNAS', 'XNYS', 'XASE'}
LIMITATIONS = [
    '股票池为当前美国普通股按 SIC 筛选的科技行业，和 QuantConnect 的 Morningstar 股票池不同。',
    '动量价格仅拆股复权，不含股息复权；不能把本次结果当作原 QC 策略的同口径结果。',
    '财报采用 filing_date 截止前一完整交易日；当前查询的修订值不能证明历史时点可见。',
    '当前财报覆盖不等于含退市公司的历史覆盖；此文件不能用来回测过去的选股。',
    '日线对齐 Massive 的 SPY 参考交易日；成交额使用未复权收盘价 × 成交量估计。',
    '选股文件没有券商实时买卖报价，仅供研究预览；模拟盘下单需另行接入并校验执行快照。',
]


def source_settings(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (not isinstance(raw, dict) or raw.get('schema_version') != 1
            or raw.get('universe_mode') not in {'us_tech_sic', 'explicit_tickers'}
            or not isinstance(raw.get('sic_industries'), dict) or not raw['sic_industries']
            or any(not re.fullmatch(r'\d{4}', k) or not isinstance(v, str) or not v
                   for k, v in raw['sic_industries'].items())
            or any(type(raw.get(k)) is not int or not lo <= raw[k] <= hi for k, lo, hi in (
                ('max_reference_symbols', 1, 20000), ('max_technology_symbols', 1, 5000),
                ('max_requests_per_run', 1, 30000), ('metadata_cache_days', 1, 30)))):
        raise DataIssue('Massive 数据源配置无效。')
    if raw['universe_mode'] == 'explicit_tickers':
        tickers = raw.get('tickers')
        if (not isinstance(tickers, list) or not tickers or len(set(tickers)) != len(tickers)
                or len(tickers) > raw['max_reference_symbols']
                or any(not isinstance(t, str) or not TICKER.fullmatch(t) for t in tickers)):
            raise DataIssue('Massive 自定义股票池格式无效。')
    elif raw.get('tickers'):
        raise DataIssue('完整 SIC 模式不能同时配置自定义股票列表。')
    return raw


def bar_days(saved, start, end):
    rows = saved['data']['results']
    if not isinstance(rows, list):
        raise DataIssue('invalid_daily_response')
    by_day = {}
    for row in rows:
        stamp = finite(row['t'])
        day = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(NY).date()
        if day in by_day or not start <= day <= end:
            raise DataIssue('duplicate_or_out_of_range_daily_bar')
        close, volume = finite(row['c']), finite(row['v'])
        if close <= 0 or volume < 0:
            raise DataIssue('invalid_price_or_volume')
        by_day[day] = row
    return by_day


def aligned_prices(adjusted, raw, sessions, start, end):
    adjusted_by_day, raw_by_day = bar_days(adjusted, start, end), bar_days(raw, start, end)
    if any(day not in adjusted_by_day or day not in raw_by_day for day in sessions):
        raise DataIssue('incomplete_daily_history')
    # Both requests must describe the same sessions; never mix instruments or
    # fill unavailable bars with zero/forward-filled prices.
    prices = [finite(adjusted_by_day[d]['c']) for d in sessions]
    raw_prices = [finite(raw_by_day[d]['c']) for d in sessions]
    volumes = [finite(raw_by_day[d]['v']) for d in sessions]
    dollars = [p * v for p, v in zip(raw_prices[-20:], volumes[-20:])]
    if any(not 0 < finite(v) for v in dollars):
        raise DataIssue('nonpositive_dollar_volume')
    return prices, raw_prices, volumes, dollars


def latest_income(rows, ticker, cik, as_of):
    eligible = []
    for row in rows:
        if (ticker not in row.get('tickers', []) or row.get('timeframe') != 'trailing_twelve_months'
                or str(row.get('cik', '')).lstrip('0') != str(cik).lstrip('0')):
            raise DataIssue('financial_identity_or_timeframe_mismatch')
        end, filing = date.fromisoformat(row['period_end']), date.fromisoformat(row['filing_date'])
        if end > filing or filing > as_of:
            raise DataIssue('financial_after_cutoff')
        if type(row.get('fiscal_quarter')) is not int or row['fiscal_quarter'] not in (1, 2, 3, 4):
            raise DataIssue('invalid_financial_period')
        eligible.append((end, filing, finite(row['operating_income'])))
    if not eligible:
        raise DataIssue('no_ttm_operating_income')
    newest = max((end, filing) for end, filing, _ in eligible)
    values = {v for end, filing, v in eligible if (end, filing) == newest}
    if len(values) != 1 or (as_of - newest[0]).days > 200:
        raise DataIssue('conflicting_or_stale_ttm_income')
    return {'value': next(iter(values)), 'period_end': newest[0].isoformat(),
            'filing_date': newest[1].isoformat(), 'timeframe': 'trailing_twelve_months',
            'original_publication_time_verified': False, 'currency': 'provider_reporting_currency'}


def income_rows(client, ticker, as_of):
    return client.pages(INCOME, {'tickers': ticker, 'timeframe': 'trailing_twelve_months',
                               'filing_date.lte': as_of.isoformat(), 'sort': 'period_end.desc', 'limit': 100},
                        max_rows=500, max_pages=10)


def reference_history(client, now, count):
    today = now.astimezone(NY).date()
    start, end = today - timedelta(days=max(600, count * 2)), today - timedelta(days=1)
    saved = client.daily('SPY', start.isoformat(), end.isoformat())
    sessions = sorted(bar_days(saved, start, end))[-count:]
    if len(sessions) != count or (today - sessions[-1]).days > 7:
        raise DataIssue('Massive 日线权限、历史长度或更新时间不足，无法建立参考交易日。')
    return start, sessions[-1], sessions


def preflight(client, now, *, count=253):
    """Check actual endpoint access before thousands of universe requests."""
    previous_cache_policy = client.read_cache
    client.read_cache = False
    try:
        start, end, sessions = reference_history(client, now, count)
        detail = client.request(TICKERS + '/AAPL')['data']['results']
        if detail.get('ticker') != 'AAPL':
            raise DataIssue('preflight_reference_identity_mismatch')
        reports = income_rows(client, 'AAPL', end)['results']
        latest_income(reports, 'AAPL', detail['cik'], end)
        return start, end, sessions
    finally:
        client.read_cache = previous_cache_policy


class MassiveTechnologyData:
    def __init__(self, config_dir, source_config, cache_dir):
        self.config_dir, self.source_config = Path(config_dir), Path(source_config)
        self.cache_dir = Path(cache_dir)

    def inspect(self, *, price_bars=253):
        config = settings(self.config_dir)
        with collection_lock(self.cache_dir):
            api = MassiveClient(config, self.cache_dir, max_requests=20)
            _, end, sessions = preflight(api, utcnow(), count=price_bars)
            return {'provider': 'massive', 'price_as_of': end.isoformat(), 'history_bars': len(sessions),
                    'daily_bars': 'available', 'ttm_operating_income': 'available',
                    'requests_per_minute': config.requests_per_minute,
                    'network_requests': api.requests, 'cache_hits': api.hits, 'execution_allowed': False}

    def collect(self, out, *, price_bars=253, progress=None):
        if type(price_bars) is not int or not 21 <= price_bars <= 700:
            raise DataIssue('unsupported_history_requirement')
        config, source = settings(self.config_dir), source_settings(self.source_config)
        with collection_lock(self.cache_dir):
            cancel = self.cache_dir / 'cancel.request'
            cancel.unlink(missing_ok=True)
            api = MassiveClient(config, self.cache_dir, max_requests=source['max_requests_per_run'],
                                cancelled=cancel.exists)
            out = Path(out)
            out.mkdir(parents=True, exist_ok=False)
            started = utcnow()

            def notify(message):
                write_json(out / 'progress.json', {'message': message, 'updated_at': utcnow().isoformat(),
                                                  'network_requests': api.requests, 'cache_hits': api.hits})
                if progress:
                    progress(message)

            try:
                notify('检查 Massive 日线和财报权限…')
                start, end, sessions = preflight(api, started, count=price_bars)
                notify('读取 Massive 当前美国普通股目录…')
                if source['universe_mode'] == 'explicit_tickers':
                    symbols = sorted(source['tickers'])
                else:
                    universe = api.pages(TICKERS, {'market': 'stocks', 'locale': 'us', 'type': 'CS',
                        'active': 'true', 'limit': 1000, 'sort': 'ticker', 'order': 'asc'},
                        max_rows=source['max_reference_symbols'])['results']
                    symbols = []
                    for row in universe:
                        symbol = row.get('ticker')
                        if not isinstance(symbol, str) or not TICKER.fullmatch(symbol) or symbol in symbols:
                            raise DataIssue('invalid_or_duplicate_reference_ticker')
                        if row.get('type') != 'CS' or row.get('market') != 'stocks' or row.get('locale') != 'us' or row.get('active') is not True:
                            raise DataIssue('unexpected_reference_universe')
                        if row.get('primary_exchange') in EXCHANGES:
                            symbols.append(symbol)
                if not symbols:
                    raise DataIssue('empty_reference_universe')
                notify(f'核对 {len(symbols)} 只股票的 SIC 行业；首次扫描较慢，速度 {config.requests_per_minute} 次/分钟，可停止后继续。')
                candidates, excluded, issues = [], [], []
                ids = set()
                for number, symbol in enumerate(symbols, 1):
                    notify(f'行业扫描 {number}/{len(symbols)} · {symbol} · 已缓存 {api.hits}')
                    try:
                        saved = api.request(TICKERS + '/' + symbol, cache_seconds=source['metadata_cache_days'] * 86400)
                        detail = saved['data']['results']
                        if detail.get('ticker') != symbol:
                            raise DataIssue('ticker_detail_identity_mismatch')
                        if (detail.get('type') != 'CS' or detail.get('locale') != 'us'
                                or detail.get('market') != 'stocks' or detail.get('active') is not True
                                or detail.get('primary_exchange') not in EXCHANGES
                                or str(detail.get('currency_name', '')).lower() != 'usd'):
                            excluded.append({'ticker': symbol, 'reason': 'not_active_us_usd_common_stock'})
                            continue
                        sic = detail.get('sic_code')
                        if not sic:
                            raise DataIssue('missing_sic_classification')
                        if sic not in source['sic_industries']:
                            excluded.append({'ticker': symbol, 'reason': 'outside_configured_sic_technology'})
                            continue
                        figi, cik = detail.get('share_class_figi'), detail.get('cik')
                        if not isinstance(figi, str) or not figi or not str(cik or '').isdigit():
                            raise DataIssue('missing_stable_security_identity')
                        if figi in ids:
                            raise DataIssue('duplicate_share_class_identity')
                        ids.add(figi)
                        if detail.get('list_date') and date.fromisoformat(detail['list_date']) > sessions[0]:
                            excluded.append({'ticker': symbol, 'reason': 'listing_history_too_short'})
                            continue
                        candidates.append((detail, saved['observed_at']))
                        if len(candidates) > source['max_technology_symbols']:
                            raise MassiveError('universe_limit', '科技股票池超过配置上限，未进行截断。')
                    except MassiveError as exc:
                        if exc.code != 'not_found':
                            raise
                        issues.append({'ticker': symbol, 'reason': exc.code})
                    except (DataIssue, ValueError, KeyError, TypeError, OverflowError) as exc:
                        reason = str(exc) if isinstance(exc, DataIssue) else 'invalid_reference_data'
                        issues.append({'ticker': symbol, 'reason': reason})
                if not candidates:
                    raise DataIssue('Massive 没有找到符合配置且历史足够的科技股。')
                stocks = []
                for number, (detail, metadata_time) in enumerate(candidates, 1):
                    symbol = detail['ticker']
                    notify(f'下载科技股 {number}/{len(candidates)} · {symbol} · 已完成 {len(stocks)}')
                    try:
                        reports = income_rows(api, symbol, end)
                        income = latest_income(reports['results'], symbol, detail['cik'], end)
                        adjusted = api.daily(symbol, start.isoformat(), end.isoformat())
                        raw = api.daily(symbol, start.isoformat(), end.isoformat(), adjusted=False)
                        prices, raw_prices, volumes, dollars = aligned_prices(adjusted, raw, sessions, start, end)
                        observed = max(metadata_time, reports['observed_at'], adjusted['observed_at'], raw['observed_at'])
                        stocks.append({'asset_id': 'FIGI:' + detail['share_class_figi'], 'ticker': symbol,
                            'industry': 'SIC:' + detail['sic_code'],
                            'industry_name': source['sic_industries'][detail['sic_code']],
                            'observed_at': observed, 'metadata_observed_at': metadata_time,
                            'financial_observed_at': reports['observed_at'], 'price_as_of': end.isoformat(),
                            'operating_income_ttm': income['value'], 'financials': income,
                            'adjusted_closes': prices, 'raw_closes': raw_prices, 'volumes': volumes,
                            'dollar_volumes': dollars, 'currency': 'USD', 'cik': detail['cik'],
                            'price_basis': 'split_adjusted_only', 'dollar_volume_basis': 'raw_close_times_volume'})
                    except MassiveError as exc:
                        if exc.code != 'not_found':
                            raise
                        issues.append({'ticker': symbol, 'reason': exc.code})
                    except (DataIssue, ValueError, KeyError, TypeError, OverflowError) as exc:
                        reason = str(exc) if isinstance(exc, DataIssue) else 'invalid_stock_data'
                        issues.append({'ticker': symbol, 'reason': reason})
                api.checkpoint()
                if not stocks:
                    raise DataIssue('Massive 采集没有可用股票；请检查 progress.json 和权限。')
                result = {'schema': SCHEMA, 'provider': 'massive', 'execution_allowed': False,
                    'synthetic': False, 'historical_point_in_time': False, 'quotes': [],
                    'universe_id': 'massive_current_' + source['universe_mode'] + '_v1',
                    'reference_count': len(symbols), 'candidate_count': len(candidates),
                    'collection_complete': not issues, 'stocks': stocks, 'issues': issues, 'excluded': excluded,
                    'started_at': started.isoformat(), 'finished_at': utcnow().isoformat(),
                    'price_as_of': end.isoformat(), 'sessions': [d.isoformat() for d in sessions],
                    'source_configuration': source, 'limitations': LIMITATIONS,
                    'network_requests': api.requests, 'cache_hits': api.hits}
                if source['universe_mode'] == 'explicit_tickers':
                    result['limitations'] = [*LIMITATIONS, '本次使用手工指定股票列表，不代表完整美国科技股池。']
                write_json(out / 'preview-data.json', result)
                notify(f'Massive 采集结束：有效 {len(stocks)} / 科技股候选 {len(candidates)}；数据问题 {len(issues)}。')
                return out / 'preview-data.json'
            except Exception as exc:
                message = str(exc) if isinstance(exc, DataIssue) else '下载中断，缓存已保留。'
                write_json(out / 'failure.json', {'status': 'incomplete', 'message': message,
                                                 'network_requests': api.requests, 'cache_hits': api.hits})
                notify(message)
                raise
