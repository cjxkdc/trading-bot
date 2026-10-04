"""Current all-sector US common-stock data for momentum research, no income gate.

Uses the existing authenticated, bounded Massive transport. Current metadata and
split-only prices are declared research inputs, never historical PIT/execution.
"""
from datetime import date
import json
from pathlib import Path

from data.common import DataIssue, collection_lock, finite, utcnow, write_json
from data.massive import TICKERS, TICKER, MassiveClient, MassiveError, settings
from data.massive_technology import EXCHANGES, aligned_prices, reference_history
from data.equity_momentum import RESEARCH_SCHEMA

LIMITATIONS = [
    '当前全美股普通股池按 SIC 行业分类；QC 使用历史 Morningstar 行业组，选股不保证相同。',
    '价格仅拆股复权、不含股息复权；QC 使用含拆股与股息的复权价格。',
    '市值及行业来自当前资料，不能作为历史时点数据回测；本模块不要求盈利或财报权限。',
    '仅供研究预览，无券商实时报价和已核对持仓，不能直接下单。',
]


def source_settings(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (set(raw) != {'schema_version', 'max_reference_symbols', 'max_requests_per_run', 'metadata_cache_days'}
            or raw['schema_version'] != 1
            or any(type(raw[k]) is not int or not lo <= raw[k] <= hi for k, lo, hi in (
                ('max_reference_symbols', 1, 20000), ('max_requests_per_run', 1, 60000),
                ('metadata_cache_days', 1, 7)))):
        raise DataIssue('全美股动量数据配置无效。')
    return raw


class MassiveMomentumData:
    def __init__(self, config_dir, source_config, cache_dir):
        self.config_dir, self.source_config = Path(config_dir), Path(source_config)
        self.cache_dir = Path(cache_dir)

    def inspect(self, *, price_bars=253):
        config = settings(self.config_dir)
        with collection_lock(self.cache_dir):
            api = MassiveClient(config, self.cache_dir, max_requests=20)
            api.read_cache = False
            _, end, sessions = reference_history(api, utcnow(), price_bars)
            detail = api.request(TICKERS + '/AAPL')['data']['results']
            if (detail.get('ticker') != 'AAPL' or not detail.get('sic_code')
                    or finite(detail.get('market_cap')) <= 0):
                raise DataIssue('市场资料缺少市值或行业。')
            return {'provider': 'massive', 'price_as_of': str(end), 'history_bars': len(sessions),
                    'market_cap': 'available', 'industry': 'available', 'income_required': False,
                    'execution_allowed': False, 'network_requests': api.requests}

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
                notify('检查日线历史，读取全美股普通股目录…')
                start, end, sessions = reference_history(api, started, price_bars)
                universe = api.pages(TICKERS, {'market': 'stocks', 'locale': 'us', 'type': 'CS',
                    'active': 'true', 'limit': 1000, 'sort': 'ticker', 'order': 'asc'},
                    max_rows=source['max_reference_symbols'])['results']
                symbols = []
                for row in universe:
                    symbol = row.get('ticker')
                    if (not isinstance(symbol, str) or not TICKER.fullmatch(symbol) or symbol in symbols
                            or row.get('type') != 'CS' or row.get('market') != 'stocks'
                            or row.get('locale') != 'us' or row.get('active') is not True):
                        raise DataIssue('invalid_common_stock_universe')
                    if row.get('primary_exchange') in EXCHANGES:
                        symbols.append(symbol)
                if not symbols:
                    raise DataIssue('empty_common_stock_universe')
                stocks, issues, excluded, ids = [], [], [], set()
                for index, symbol in enumerate(symbols, 1):
                    notify(f'全美股 {index}/{len(symbols)} · {symbol} · 有效 {len(stocks)} · 缓存 {api.hits}')
                    try:
                        saved = api.request(TICKERS + '/' + symbol,
                                            cache_seconds=source['metadata_cache_days'] * 86400)
                        detail = saved['data']['results']
                        if detail.get('ticker') != symbol:
                            raise DataIssue('ticker_detail_identity_mismatch')
                        if (detail.get('type') != 'CS' or detail.get('locale') != 'us'
                                or detail.get('market') != 'stocks' or detail.get('active') is not True
                                or detail.get('primary_exchange') not in EXCHANGES
                                or str(detail.get('currency_name', '')).lower() != 'usd'):
                            excluded.append({'ticker': symbol, 'reason': 'not_listed_us_usd_common_stock'})
                            continue
                        figi, sic = detail.get('share_class_figi'), str(detail.get('sic_code', ''))
                        cap = finite(detail['market_cap'])
                        if not isinstance(figi, str) or not figi or not (len(sic) == 4 and sic.isdigit()) or cap <= 0:
                            raise DataIssue('missing_identity_cap_or_sic')
                        if figi in ids:
                            raise DataIssue('duplicate_share_class_identity')
                        ids.add(figi)
                        if detail.get('list_date') and date.fromisoformat(detail['list_date']) > sessions[0]:
                            excluded.append({'ticker': symbol, 'reason': 'listing_history_too_short'})
                            continue
                        adjusted = api.daily(symbol, str(start), str(end))
                        raw = api.daily(symbol, str(start), str(end), adjusted=False)
                        prices, raw_prices, volumes, dollars = aligned_prices(adjusted, raw, sessions, start, end)
                        scale = prices[-1] / raw_prices[-1]
                        # Derive the split factor from raw/split-only closes;
                        # do not assume the provider also adjusted its v field.
                        shares = [v * r / p * scale for v, r, p in
                                  zip(volumes[-20:], raw_prices[-20:], prices[-20:])]
                        stocks.append({'asset_id': 'FIGI:' + figi, 'ticker': symbol, 'industry': 'SIC:' + sic,
                            'observed_at': max(saved['observed_at'], adjusted['observed_at'], raw['observed_at']),
                            'metadata_observed_at': saved['observed_at'], 'price_as_of': str(end),
                            'market_cap': cap, 'raw_close': raw_prices[-1], 'share_volumes': shares,
                            'adjusted_closes': prices, 'dollar_volumes': dollars, 'price_basis': 'split_adjusted_only'})
                    except MassiveError as exc:
                        if exc.code != 'not_found':
                            raise
                        issues.append({'ticker': symbol, 'reason': exc.code})
                    except (ValueError, KeyError, TypeError, OverflowError):
                        issues.append({'ticker': symbol, 'reason': 'incomplete_or_invalid_stock_data'})
                api.checkpoint()
                if not stocks:
                    raise DataIssue('全美股采集没有可用股票，请检查数据权限及覆盖范围。')
                result = {'schema': RESEARCH_SCHEMA, 'provider': 'massive', 'execution_allowed': False,
                    'synthetic': False, 'historical_point_in_time': False, 'quotes': [],
                    'universe_scope': 'us_listed_common_stock', 'candidate_count': len(symbols),
                    'collection_complete': not issues, 'stocks': stocks, 'issues': issues, 'excluded': excluded,
                    'started_at': started.isoformat(), 'finished_at': utcnow().isoformat(), 'price_as_of': str(end),
                    'sessions': [str(d) for d in sessions], 'limitations': LIMITATIONS,
                    'network_requests': api.requests, 'cache_hits': api.hits, 'source_configuration': source}
                content = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
                if len(content) > 32 * 1024 * 1024:
                    raise DataIssue('完整快照超过 32 MB，未截断股票池；请降低历史长度。')
                path = out / 'preview-data.json'
                path.write_bytes(content)
                notify(f'全美股研究数据已完成：有效 {len(stocks)}/{len(symbols)}，数据问题 {len(issues)}。')
                return path
            except Exception as exc:
                message = str(exc) if isinstance(exc, DataIssue) else '下载中断，缓存已保留。'
                write_json(out / 'failure.json', {'status': 'incomplete', 'message': message})
                notify(message)
                raise
