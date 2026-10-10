"""One Massive provider. Universe and requested fields come from the strategy."""
from datetime import date, datetime, time
import json
from math import isfinite
from pathlib import Path

from data.common import DataIssue, collection_lock, describe_issue, finite, utcnow, write_json
from data.massive import TICKER, TICKERS, MassiveClient, MassiveError, settings
from data.massive_technology import EXCHANGES, NY, income_rows, latest_income, reference_history
from data.massive_batch import grouped_rows, market_histories, split_events
from framework.data_requirements import DataRequirements
from framework.portfolio import EquitySnapshot, StockSnapshot

SCHEMA = 'current_us_equity_request_v1'
FIELDS = {'industry', 'adjusted_closes', 'dollar_volumes', 'operating_income_ttm',
          'market_cap', 'raw_close', 'share_volumes'}
SCHEMAS = {'us_technology_daily_v1', 'us_equity_momentum_daily_v1'}
LIMITATIONS = [
    '当前股票池及当前资料仅供研究，不能冒充历史时点股票池或财报。',
    '行业按 SIC 分类，与 QuantConnect 的 Morningstar 分类不完全相同。',
    '价格仅拆股复权，不含股息复权；成交额按未复权收盘价乘成交量估计。',
    '没有券商实时买卖报价或已核对持仓，此研究文件不能直接下单。',
]
BACKFILL_LIMITATION = ('历史回填：股票池与基础资料按供应商的时点参数查询，财报按申报日截止；市值等字段是否严格为该日时点取决于供应商，'
                       '当日已退市的股票可能缺失，不能当作完整的时点数据。')
BACKFILL_LIMITATION = ('历史回填：股票池与基础资料按供应商的时点参数查询，财报按申报日截止；市值等字段是否严格为该日时点取决于供应商，'
                       '当日已退市的股票可能缺失，不能当作完整的时点数据。')
BACKFILL_LIMITATION = ('历史回填：股票池与基础资料按供应商的时点参数查询，财报按申报日截止；市值等字段是否严格为该日时点取决于供应商，'
                       '当日已退市的股票可能缺失，不能当作完整的时点数据。')


def validate_request(request):
    if not isinstance(request, DataRequirements):
        raise DataIssue('请先由策略声明所需数据。')
    if (request.market != 'us' or request.security_type != 'common_stock'
            or request.frequency != 'daily' or request.universe not in {'technology', 'all'}
            or request.input_schema not in SCHEMAS):
        raise DataIssue('Massive 当前接入仅支持美国普通股日线及已适配的股票范围。')
    if set(request.fields) - FIELDS or 'adjusted_closes' not in request.fields:
        raise DataIssue('Massive 当前接入不能提供策略要求的全部字段。')
    if not 21 <= max(request.price_bars, request.volume_bars) <= 700:
        raise DataIssue('Massive 当前接入支持 21–700 个交易日的历史请求。')
    if request.input_schema == 'us_equity_momentum_daily_v1' and not {
            'industry', 'market_cap', 'raw_close', 'share_volumes'} <= set(request.fields):
        raise DataIssue('股票输入契约缺少市值、价格、成交股数或行业需求。')


def source_settings(path):
    source = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (not isinstance(source, dict) or set(source) != {'schema_version', 'max_reference_symbols',
            'max_requests_per_run', 'metadata_cache_days', 'technology_sic_industries'}
            or source['schema_version'] != 1
            or any(type(source[k]) is not int or not low <= source[k] <= high for k, low, high in (
                ('max_reference_symbols', 1, 20000), ('max_requests_per_run', 1, 60000),
                ('metadata_cache_days', 1, 7)))
            or not isinstance(source['technology_sic_industries'], dict)
            or not source['technology_sic_industries']
            or any(not isinstance(k, str) or len(k) != 4 or not k.isdigit()
                   or not isinstance(v, str) or not v for k, v in source['technology_sic_industries'].items())):
        raise DataIssue('Massive 供应商配置无效。')
    return source


class MassiveData:
    def __init__(self, config_dir, source_config, cache_dir):
        self.config_dir, self.source_config, self.cache_dir = map(Path, (config_dir, source_config, cache_dir))
        self.request = None

    def for_request(self, request):
        validate_request(request)
        self.request = request
        return self

    def preflight(self, api, now):
        request = self.request
        validate_request(request)
        start, end, sessions = reference_history(api, now, max(request.price_bars, request.volume_bars))
        detail = api.request(TICKERS + '/AAPL')['data']['results']
        if detail.get('ticker') != 'AAPL':
            raise DataIssue('preflight_reference_identity_mismatch')
        if 'industry' in request.fields and not detail.get('sic_code'):
            raise DataIssue('missing_sic_classification')
        if 'market_cap' in request.fields and finite(detail.get('market_cap')) <= 0:
            raise DataIssue('missing_market_cap')
        if 'operating_income_ttm' in request.fields:
            reports = income_rows(api, 'AAPL', end)
            latest_income(reports['results'], 'AAPL', detail['cik'], end)
        # Probe the exact batch/action endpoints before beginning a long job.
        grouped_rows(api.grouped_daily(str(end)), end, {'SPY'})
        split_events(api, sessions[0], end)
        return start, end, sessions

    def inspect(self, *, price_bars=None):
        validate_request(self.request)
        config = settings(self.config_dir)
        with collection_lock(self.cache_dir):
            api = MassiveClient(config, self.cache_dir, max_requests=20)
            api.read_cache = False
            _, end, sessions = self.preflight(api, utcnow())
            return {'provider': 'massive', 'request': self.request.record(), 'price_as_of': str(end),
                    'history_bars': len(sessions), 'income_required': 'operating_income_ttm' in self.request.fields,
                    'execution_allowed': False, 'network_requests': api.requests}

    def collect(self, out, *, price_bars=None, progress=None, as_of=None):
        request = self.request
        validate_request(request)
        if as_of is not None and (type(as_of) is not date or as_of >= utcnow().astimezone(NY).date()):
            raise DataIssue('回填日期必须是早于今天的日期。')
        config, source = settings(self.config_dir), source_settings(self.source_config)
        with collection_lock(self.cache_dir):
            cancel = self.cache_dir / 'cancel.request'
            cancel.unlink(missing_ok=True)
            api = MassiveClient(config, self.cache_dir, max_requests=source['max_requests_per_run'], cancelled=cancel.exists)
            out = Path(out)
            out.mkdir(parents=True, exist_ok=False)
            started = utcnow()
            # A historical anchor ends the reference sessions the day before as_of; cache freshness still follows the clock.
            anchor = datetime.combine(as_of, time(12), tzinfo=NY) if as_of else started
            prefix = f'回填 {as_of} · ' if as_of else ''
            def notify(message):
                message = prefix + message
                write_json(out/'progress.json', {'message': message, 'updated_at': utcnow().isoformat(),
                    'network_requests': api.requests, 'cache_hits': api.hits})
                if progress: progress(message)
            try:
                notify('按策略需求检查数据权限…')
                api.read_cache = False
                start, end, sessions = self.preflight(api, anchor)
                api.read_cache = True
                listing = {'market':'stocks', 'locale':'us', 'type':'CS', 'active':'true', 'limit':1000, 'sort':'ticker', 'order':'asc'}
                if as_of:
                    listing['date'] = str(end)  # the vendor's point-in-time listing for the signal date
                universe = api.pages(TICKERS, listing, max_rows=source['max_reference_symbols'])['results']
                symbols, seen = [], set()
                for row in universe:
                    symbol = row.get('ticker')
                    if (not isinstance(symbol, str) or not TICKER.fullmatch(symbol) or symbol in seen
                            or row.get('type') != 'CS' or row.get('market') != 'stocks'
                            or row.get('locale') != 'us' or row.get('active') is not True):
                        raise DataIssue('invalid_or_duplicate_reference_ticker')
                    seen.add(symbol)
                    if row.get('primary_exchange') in EXCHANGES: symbols.append(symbol)
                if not symbols: raise DataIssue('empty_reference_universe')
                notify(f'股票池 {len(symbols)} 只；按交易日批量采集 {len(sessions)} 天行情。')
                histories, price_observed, excluded, issues = market_histories(
                    api, sessions, symbols, request, started, notify)
                policy = request.collection_policy
                if policy is not None:
                    ranked = []
                    for symbol, history in histories.items():
                        api.checkpoint()
                        score = policy.priority(history)
                        if score is None:
                            excluded.append({'ticker': symbol, 'reason': 'strategy_price_screen'})
                        elif isinstance(score, bool) or not isinstance(score, (int, float)) or not isfinite(score):
                            raise DataIssue('invalid_strategy_collection_priority')
                        else:
                            ranked.append((score, symbol))
                    ranked.sort(key=lambda item: (-item[0], item[1]))
                    ordered = [symbol for _, symbol in ranked]
                    notify(f'策略初筛 {len(ordered)}/{len(symbols)} 只；按排名补查基础资料。')
                else:
                    ordered = [s for s in symbols if s in histories]
                    notify(f'完整历史 {len(ordered)}/{len(symbols)} 只；按需求补查基础资料。')
                stocks, hydrated, ids, candidates, details_checked, prefix_complete = [], [], set(), 0, 0, False
                for index, symbol in enumerate(ordered, 1):
                    api.checkpoint()
                    notify(f'候选资料 {index}/{len(ordered)} · {symbol} · 有效 {len(stocks)} · 缓存 {api.hits}')
                    details_checked += 1
                    try:
                        saved = (api.request(TICKERS+'/'+symbol, {'date': str(end)}, cache_seconds=90*86400) if as_of
                                 else api.request(TICKERS+'/'+symbol, cache_seconds=source['metadata_cache_days']*86400))
                        detail = saved['data']['results']
                        if detail.get('ticker') != symbol: raise DataIssue('ticker_detail_identity_mismatch')
                        if (detail.get('type') != 'CS' or detail.get('locale') != 'us' or detail.get('market') != 'stocks'
                                or detail.get('active') is not True or detail.get('primary_exchange') not in EXCHANGES
                                or str(detail.get('currency_name', '')).lower() != 'usd'):
                            excluded.append({'ticker':symbol, 'reason':'not_active_us_usd_common_stock'})
                            continue
                        sic = str(detail.get('sic_code') or '')
                        if (request.universe == 'technology' or 'industry' in request.fields) and (len(sic)!=4 or not sic.isdigit()):
                            raise DataIssue('missing_sic_classification')
                        if request.universe == 'technology' and sic not in source['technology_sic_industries']:
                            excluded.append({'ticker':symbol, 'reason':'outside_requested_universe'})
                            continue
                        figi = detail.get('share_class_figi')
                        if not isinstance(figi,str) or not figi: raise DataIssue('missing_stable_security_identity')
                        if figi in ids: raise DataIssue('duplicate_share_class_identity')
                        ids.add(figi)
                        if detail.get('list_date') and date.fromisoformat(detail['list_date']) > sessions[0]:
                            excluded.append({'ticker':symbol, 'reason':'listing_history_too_short'})
                            continue
                        candidates += 1
                        stock = {'asset_id':'FIGI:'+figi, 'ticker':symbol, 'observed_at':max(saved['observed_at'], price_observed),
                                 'metadata_observed_at':saved['observed_at'], 'price_as_of':str(end)}
                        if 'industry' in request.fields: stock['industry'] = 'SIC:'+sic
                        if 'market_cap' in request.fields:
                            stock['market_cap'] = finite(detail['market_cap'])
                            if stock['market_cap'] <= 0: raise DataIssue('invalid_market_cap')
                        if 'operating_income_ttm' in request.fields:
                            reports = income_rows(api, symbol, end)
                            income = latest_income(reports['results'], symbol, detail['cik'], end)
                            stock.update(operating_income_ttm=income['value'], financials=income)
                            stock['observed_at'] = max(stock['observed_at'], reports['observed_at'])
                        history = histories[symbol]
                        stock.update(adjusted_closes=list(history.adjusted_closes), price_basis='split_adjusted_only')
                        for field in ('raw_close', 'dollar_volumes', 'share_volumes'):
                            if field in request.fields:
                                value = getattr(history, field)
                                stock[field] = list(value) if isinstance(value, tuple) else value
                        stocks.append(stock)
                        if policy is not None:
                            common = (stock['asset_id'], symbol, datetime.fromisoformat(stock['observed_at']).date(), end,
                                      stock.get('industry', ''), stock.get('operating_income_ttm', float('nan')),
                                      tuple(stock['adjusted_closes']), tuple(stock.get('dollar_volumes', ())))
                            hydrated.append(EquitySnapshot(*common, stock['market_cap'], stock['raw_close'],
                                                           tuple(stock['share_volumes']))
                                            if request.input_schema == 'us_equity_momentum_daily_v1' else StockSnapshot(*common))
                            complete = policy.complete(tuple(hydrated))
                            if type(complete) is not bool:
                                raise DataIssue('invalid_strategy_collection_completion')
                            if complete:
                                prefix_complete = True
                                break
                    except MassiveError as exc:
                        if exc.code != 'not_found': raise
                        issues.append({'ticker':symbol,'reason':exc.code})
                    except (DataIssue,ValueError,KeyError,TypeError,OverflowError) as exc:
                        issues.append({'ticker':symbol,'reason':str(exc) if isinstance(exc,DataIssue) else 'incomplete_or_invalid_stock_data'})
                api.checkpoint()
                if not stocks: raise DataIssue('没有取得满足策略需求的股票数据，请检查权限及覆盖范围。')
                coverage = {'method': 'ranked_prefix_v1' if policy else 'full',
                            'history_eligible_count': len(histories), 'price_candidate_count': len(ordered),
                            'details_checked': details_checked, 'details_remaining': len(ordered)-details_checked,
                            'ranked_prefix_complete': prefix_complete, 'all_candidates_checked': details_checked == len(ordered)}
                result = {'schema':SCHEMA,'provider':'massive','request':request.record(),'request_id':request.fingerprint,
                    'execution_allowed':False,'synthetic':False,'historical_point_in_time':False,'quotes':[],
                    'as_of_requested':str(as_of) if as_of else None,
                    'as_of_requested':str(as_of) if as_of else None,
                    'as_of_requested':str(as_of) if as_of else None,
                    'reference_count':len(symbols),'candidate_count':len(ordered) if policy else candidates,
                    'collection_complete':not issues, 'coverage':coverage,
                    'stocks':stocks,'issues':issues,'excluded':excluded,'started_at':started.isoformat(),
                    'finished_at':utcnow().isoformat(),'price_as_of':str(end),'sessions':[str(d) for d in sessions],
                    'limitations':LIMITATIONS + (['仅补齐策略所需排名前缀的基础资料，未下载全股票池资料。'] if policy else [])
                                  + ([BACKFILL_LIMITATION] if as_of else []),
                    'source_configuration':source,'network_requests':api.requests,'cache_hits':api.hits}
                content=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
                if len(content)>32*1024*1024: raise DataIssue('完整快照超过 32 MB；未截断股票池。')
                path=out/'preview-data.json'
                path.write_bytes(content)
                notify(f'采集完成：{len(sessions)} 天批量行情，资料 {details_checked}/{len(ordered)}，问题 {len(issues)}。')
                return path
            except Exception as exc:
                message=(describe_issue(str(exc)) if isinstance(exc,DataIssue)
                         else str(exc) if isinstance(exc, MassiveError) else '下载中断，缓存已保留。')
                write_json(out/'failure.json',{'status':'incomplete','message':message})
                notify(message)
                raise
