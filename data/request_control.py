from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS
"""Bind a provider to a validated strategy request, including snapshot identity."""
from datetime import date, datetime

from app.control_contracts import ControlError, SnapshotInput
from data.common import finite
from data.controls import MassiveControl, SnapshotControl
from data.massive_provider import SCHEMA, validate_request
from framework.portfolio import EquitySnapshot, StockSnapshot


class SnapshotProviderControl(SnapshotControl):
    """One local-file source, with parsers selected by data format, never strategy name."""
    def for_request(self, request):
        self.request = request
        return self

    def validate_request(self):
        if self.request.input_schema not in {'us_technology_daily_v1', 'us_equity_momentum_daily_v1'}:
            raise ControlError('本地文件接入尚不支持策略要求的数据格式。')

    def reader(self, raw):
        self.validate_request()
        if not isinstance(raw, dict) or raw.get('input_schema') != self.request.input_schema:
            raise ControlError('文件输入格式与当前策略不一致。')
        entry = dict(self.entry, input_schema=self.request.input_schema, strategy_schema=self.request.input_schema)
        if self.request.input_schema == 'us_equity_momentum_daily_v1':
            from data.equity_momentum import EquitySnapshotControl
            return EquitySnapshotControl(self.root, self.profile, entry)
        return SnapshotControl(self.root, self.profile, entry)

    def metadata(self, raw):
        # Parse as well as inspecting the label, preserving executable-file validation.
        return self.reader(raw).load(raw).metadata

    def load(self, raw):
        return self.reader(raw).load(raw)


class MassiveProviderControl(MassiveControl):
    def paper_signal(self, raw, clock):
        from dataclasses import replace
        from zoneinfo import ZoneInfo
        self.metadata(raw)
        if raw.get('synthetic', False) is not False:
            raise ControlError('合成数据不能用于券商模拟交易。')
        try:
            finished = datetime.fromisoformat(raw['finished_at'])
            if finished.tzinfo is None or not 0 <= (clock-finished).total_seconds() <= 24*3600:
                raise ValueError('expired collection')
            observed = {s['asset_id']:datetime.fromisoformat(s['observed_at']) for s in raw['stocks']}
            if any(t.tzinfo is None or t > clock for t in observed.values()):
                raise ValueError('invalid observation')
        except (ValueError, KeyError, TypeError):
            raise ControlError('数据观察时间无效或采集已超过一天，请更新数据。') from None
        stocks = tuple(replace(s,known_on=observed[s.asset_id].astimezone(ZoneInfo('America/New_York')).date())
                       for s in self._rows(raw))
        # Current observation is explicit, never relabelled as historical PIT data.
        warnings = [x for x in raw.get('limitations', []) if '没有券商实时' not in x]
        if raw.get('issues'):
            warnings.append(f"本轮模拟仅使用资料可验证的候选；{len(raw['issues'])} 只资料异常证券未参与排名，结果可能受覆盖缺失影响。")
        return stocks, warnings
    def for_request(self, request):
        # Unsupported requests must still allow credentials to be configured.
        self.request = request
        return self

    def validate_request(self):
        validate_request(self.request)

    @property
    def directory(self):
        return super().directory / 'requests' / self.request.fingerprint

    def collector(self):
        return super().collector().for_request(self.request)

    def inspect(self, *, price_bars, progress):
        progress('正在按当前策略的范围和字段检查权限…')
        return self.collector().inspect()

    def metadata(self, raw):
        self.validate_request()
        if (not isinstance(raw, dict) or raw.get('schema') != SCHEMA
                or raw.get('execution_allowed') is not False or raw.get('historical_point_in_time') is not False
                or raw.get('request') != self.request.record() or raw.get('request_id') != self.request.fingerprint
                or not isinstance(raw.get('stocks'), list)):
            raise ControlError('快照的股票范围、字段或历史长度与当前策略需求不一致，请重新采集。')
        rows = self._rows(raw)  # Do not advertise a snapshot that only has a matching label.
        coverage = raw.get('coverage')
        if self.request.collection_policy is not None:
            if (not isinstance(coverage, dict) or coverage.get('method') != 'ranked_prefix_v1'
                    or not (coverage.get('ranked_prefix_complete') is True or coverage.get('all_candidates_checked') is True)):
                raise ControlError('快照未完成策略要求的排名前缀或完整候选检查。')
            counts = [coverage.get(k) for k in ('price_candidate_count', 'details_checked', 'details_remaining')]
            if (any(type(n) is not int or n < 0 for n in counts) or counts[0] != counts[1]+counts[2]
                    or counts[1] < len(rows) or coverage.get('all_candidates_checked') is not (counts[2] == 0)
                    or (coverage.get('ranked_prefix_complete') is True and self.request.collection_policy.complete(rows) is not True)):
                raise ControlError('快照候选覆盖或排名缓冲校验失败。')
        return {'price_as_of': raw.get('price_as_of'), 'collected_at': raw.get('finished_at'),
                'candidate_count': raw['candidate_count'], 'usable_count':len(raw['stocks']),
                'collection_complete':raw.get('collection_complete'), 'research_only':True,
                'synthetic':raw.get('synthetic',False), 'limitations':raw.get('limitations',[]),
                'coverage':coverage,
                'data_requirements':self.request.describe()}

    def _rows(self, raw):
        rows, ids = [], set()
        request = self.request
        for item in raw['stocks']:
            if any(name not in item for name in request.fields):
                raise ControlError('快照缺少策略要求的数据字段。')
            prices = tuple(finite(v) for v in item['adjusted_closes'])
            if len(prices) != request.price_bars or any(p <= 0 for p in prices):
                raise ControlError('快照价格历史不足或无效。')
            for field in ('dollar_volumes', 'share_volumes'):
                if field in request.fields:
                    values = tuple(finite(v) for v in item[field])
                    if len(values) != request.volume_bars or any(v < 0 for v in values):
                        raise ControlError('快照成交量历史不足或无效。')
            if 'industry' in request.fields and not item['industry']:
                raise ControlError('快照缺少行业信息。')
            if (not isinstance(item.get('asset_id'),str) or not item['asset_id'] or item['asset_id'] in ids
                    or not isinstance(item.get('ticker'),str) or not item['ticker']):
                raise ControlError('快照证券标识无效或重复。')
            ids.add(item['asset_id'])
            observed = datetime.fromisoformat(item['observed_at'])
            if observed.tzinfo is None or item['price_as_of'] != raw['price_as_of']:
                raise ControlError('快照观察时间或价格日期无效。')
            common = (item['asset_id'], item['ticker'], observed.date(), date.fromisoformat(item['price_as_of']),
                      item.get('industry',''), finite(item['operating_income_ttm']) if 'operating_income_ttm' in request.fields else float('nan'),
                      prices, tuple(item.get('dollar_volumes',())))
            if request.input_schema == 'us_equity_momentum_daily_v1':
                cap, close = finite(item['market_cap']), finite(item['raw_close'])
                if cap <= 0 or close <= 0: raise ControlError('快照市值或原始股价无效。')
                rows.append(EquitySnapshot(*common, cap, close, tuple(item['share_volumes'])))
            else:
                rows.append(StockSnapshot(*common))
        return tuple(rows)

    def load(self, raw):
        meta = self.metadata(raw)
        stocks = self._rows(raw)
        return SnapshotInput(self.request.input_schema, True, raw, stocks, meta,
                             {s.asset_id:{'ticker':s.ticker,'industry':s.industry} for s in stocks}, order_requirements=US_EQUITY_LIMIT_ORDERS)
