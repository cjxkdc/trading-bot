from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS
"""US common-stock momentum snapshots; brokerage-independent data contract."""
from datetime import date, datetime
import json
from pathlib import Path

from app.control_contracts import ControlError, SnapshotInput
from data.controls import MassiveControl, SnapshotControl
from framework.portfolio import EquitySnapshot, PortfolioContext
from framework.runtime import MarketSnapshot
from framework.trading import Quote, amount

SCHEMA = 'us_equity_momentum_daily_v1'
RESEARCH_SCHEMA = 'current_us_equity_momentum_research_v1'


def equity_rows(raw, research=False):
    rows = []
    for s in raw['stocks']:
        known = (datetime.fromisoformat(s['observed_at']).date() if research
                 else date.fromisoformat(s['known_on']))
        rows.append(EquitySnapshot(s['asset_id'], s['ticker'], known, date.fromisoformat(s['price_as_of']),
            s['industry'], 0.0, tuple(s['adjusted_closes']), tuple(s['dollar_volumes']),
            float(s['market_cap']), float(s['raw_close']), tuple(s['share_volumes'])))
    if len({s.asset_id for s in rows}) != len(rows):
        raise ControlError('全美股快照包含重复证券。')
    return tuple(rows)


def parse_equity_snapshot(raw):
    if (raw.get('input_schema') != SCHEMA or raw.get('universe_complete') is not True
            or raw.get('universe_scope') != 'us_listed_common_stock'
            or raw.get('execution_allowed') is False):
        raise ControlError('MOM8 需要完整美国普通股执行快照；研究数据或科技股子集不能替代。')
    stamp = datetime.fromisoformat(raw['execution_time'])
    if stamp.tzinfo is None:
        raise ControlError('执行快照需要带时区的时间。')
    context = raw.get('portfolio_context')
    if (not isinstance(context, dict) or set(context) != {'as_of', 'held_asset_ids'}
            or not isinstance(context['held_asset_ids'], list)):
        raise ControlError('MOM8 执行快照必须包含本策略账户的已核对持仓；缺失不代表空仓。')
    context = PortfolioContext(date.fromisoformat(context['as_of']), tuple(context['held_asset_ids']))
    quotes = {}
    for q in raw['quotes']:
        if q['asset_id'] in quotes:
            raise ControlError('执行快照有重复报价。')
        quotes[q['asset_id']] = Quote(q['asset_id'], amount(q['bid'], True), amount(q['ask'], True),
                                      datetime.fromisoformat(q['as_of']))
    return MarketSnapshot(SCHEMA, equity_rows(raw), quotes, stamp, context)


class EquitySnapshotFile:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        return parse_equity_snapshot(json.loads(self.path.read_text(encoding='utf-8-sig')))


class EquitySnapshotControl(SnapshotControl):
    def load(self, raw):
        snapshot = parse_equity_snapshot(raw)
        return SnapshotInput(SCHEMA, False, raw, snapshot.stocks, self.metadata(raw),
            {s.asset_id: {'ticker': s.ticker, 'industry': s.industry} for s in snapshot.stocks}, snapshot, order_requirements=US_EQUITY_LIMIT_ORDERS)


class MassiveMomentumControl(MassiveControl):
    def inspect(self, *, price_bars, progress):
        progress('检查全美股日线、市值及行业数据权限…')
        return self.collector().inspect(price_bars=price_bars)

    def load(self, raw):
        meta = self.metadata(raw)
        if raw.get('universe_scope') != 'us_listed_common_stock':
            raise ControlError('此策略需要美国普通股池，科技股子集不能冒充全市场数据。')
        stocks = equity_rows(raw, research=True)
        return SnapshotInput(SCHEMA, True, raw, stocks, meta,
            {s.asset_id: {'ticker': s.ticker, 'industry': s.industry} for s in stocks}, order_requirements=US_EQUITY_LIMIT_ORDERS)
