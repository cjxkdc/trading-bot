# Paste this ENTIRE file into a new QuantConnect Python project's main.py.
# Generated file: edit the original modules, then rebuild.
# Source SHA256: 5086bf9f4961e1428b703d6486491e8ce06699ab02a0629204dbaef970a81f50

# ===== framework/market.py =====
"""Portable execution metadata supplied by market/broker modules.

Legacy defaults are adapters for pre-context snapshots, not platform capabilities.
Every new module should declare its execution context explicitly.
"""
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def positive(value):
    if isinstance(value, bool):
        raise ValueError('Invalid instrument rule')
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError('Instrument increments must be positive')
    return result


@dataclass(frozen=True)
class InstrumentRules:
    quantity_step: Decimal = Decimal('1')
    min_quantity: Decimal = Decimal('1')
    price_tick: Decimal = Decimal('0.01')
    contract_multiplier: Decimal = Decimal('1')

    def __post_init__(self):
        for name in ('quantity_step', 'min_quantity', 'price_tick', 'contract_multiplier'):
            object.__setattr__(self, name, positive(getattr(self, name)))
        if self.min_quantity % self.quantity_step:
            raise ValueError('Minimum quantity must align with quantity step')

    @classmethod
    def from_record(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {'quantity_step','min_quantity','price_tick','contract_multiplier'}:
            raise ValueError('Incomplete instrument rules')
        return cls(**value)

    def record(self):
        return {name: str(getattr(self, name)) for name in ('quantity_step','min_quantity','price_tick','contract_multiplier')}

    def floor_quantity(self, value):
        return (Decimal(str(value)) / self.quantity_step).to_integral_value(rounding=ROUND_FLOOR) * self.quantity_step

    def valid_quantity(self, value):
        value = Decimal(str(value))
        return value.is_finite() and value >= self.min_quantity and value % self.quantity_step == 0

    def round_price(self, value, side):
        return (positive(value) / self.price_tick).to_integral_value(
            rounding=ROUND_CEILING if side == 'BUY' else ROUND_FLOOR) * self.price_tick


@dataclass(frozen=True)
class ExecutionContext:
    market: str
    timezone: str
    currency: str
    asset_class: str
    instrument_rules: InstrumentRules = field(default_factory=InstrumentRules)

    def __post_init__(self):
        for name in ('market','timezone','currency','asset_class'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise ValueError('Invalid execution context')
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Execution timezone must be an IANA timezone') from None
        object.__setattr__(self, 'instrument_rules', InstrumentRules.from_record(self.instrument_rules))

    @classmethod
    def from_record(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) != {'market','timezone','currency','asset_class','instrument_rules'}:
            raise ValueError('Incomplete execution context')
        return cls(**value)

    def record(self):
        return dict(market=self.market, timezone=self.timezone, currency=self.currency,
                    asset_class=self.asset_class, instrument_rules=self.instrument_rules.record())

    @classmethod
    def legacy_us(cls):
        return cls("US", "America/New_York", "USD", "equity")

    def rules_for(self, symbol, quote=None):
        rule = getattr(quote, 'rules', None)
        return InstrumentRules.from_record(rule) if rule is not None else self.instrument_rules


def legacy_execution_context():
    """Compatibility only for records created before context metadata existed."""
    return ExecutionContext('US', 'America/New_York', 'USD', 'equity')


def execution_context(value=None):
    return legacy_execution_context() if value is None else ExecutionContext.from_record(value)


def rules_for(symbol, quotes, context=None):
    return execution_context(context).rules_for(symbol, quotes.get(symbol))

# ===== framework/portfolio.py =====
"""Platform-independent contract for cross-sectional, long-only portfolios."""

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Protocol, Sequence


@dataclass(frozen=True)
class HistoryRequirements:
    price_bars: int
    dollar_volume_bars: int

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.price_bars, self.dollar_volume_bars)):
            raise ValueError("History requirements must be positive numbers of bars")


@dataclass(frozen=True)
class StockSnapshot:
    asset_id: str
    ticker: str
    known_on: date
    price_as_of: date
    industry: str
    operating_income_ttm: float
    adjusted_closes: tuple[float, ...]
    dollar_volumes: tuple[float, ...]


@dataclass(frozen=True)
class EquitySnapshot(StockSnapshot):
    """Listed equity input, with explicit capitalization and share volume.

    Kept separate from the original technology contract: turnover in dollars is
    not share volume, and a missing market cap must never become a zero.
    """
    market_cap: float
    raw_close: float
    share_volumes: tuple[float, ...]


@dataclass(frozen=True)
class StockTarget:
    asset_id: str
    weight: float
    score: float


@dataclass(frozen=True)
class PortfolioContext:
    """Observed membership, frozen before selection; no broker handles or orders.

    This first contract supports retention/rank buffers. It does not provide
    valuation or quantities for no-top-up sizing policies.
    """
    as_of: date
    held_asset_ids: tuple[str, ...]

    def __post_init__(self):
        if type(self.as_of) is not date or type(self.held_asset_ids) is not tuple:
            raise ValueError("Context requires a date and an immutable tuple of held IDs")
        if any(not isinstance(s, str) or not s for s in self.held_asset_ids):
            raise ValueError("Invalid held asset identifier")
        if len(set(self.held_asset_ids)) != len(self.held_asset_ids):
            raise ValueError("Duplicate held asset identifiers")
        object.__setattr__(self, "held_asset_ids", tuple(sorted(self.held_asset_ids)))


class PortfolioStrategy(Protocol):
    @property
    def history_requirements(self) -> HistoryRequirements: ...

    def select(self, stocks: Sequence[StockSnapshot]) -> tuple[StockTarget, ...]: ...


class ContextPortfolioStrategy(Protocol):
    """Opt in with requires_context = True; stateless plug-ins stay compatible."""
    requires_context: bool

    @property
    def history_requirements(self) -> HistoryRequirements: ...

    def select_with_context(self, stocks: Sequence[StockSnapshot],
                            context: PortfolioContext) -> tuple[StockTarget, ...]: ...


class PortfolioEngine:
    """Validate information timing and targets without importing any platform."""

    def __init__(self, strategy: PortfolioStrategy, max_gross=0.95, max_weight=0.19):
        if not 0 < max_weight <= max_gross <= 1:
            raise ValueError("Invalid long-only portfolio limits")
        self.strategy = strategy
        self.max_gross, self.max_weight = max_gross, max_weight

    def plan(self, stocks: Sequence[StockSnapshot], execution_day: date, context=None, *, current_information=False):
        stocks = tuple(stocks)
        ids = {stock.asset_id for stock in stocks}
        if len(ids) != len(stocks):
            raise ValueError("Duplicate asset identifiers")
        for stock in stocks:
            cutoff = execution_day if current_information is True else stock.price_as_of
            if not stock.known_on <= cutoff or not stock.price_as_of < execution_day:
                raise ValueError("Information must precede the execution session")
        if context is not None:
            if not isinstance(context, PortfolioContext) or context.as_of > execution_day:
                raise ValueError("Invalid or future portfolio context")
            if stocks and context.as_of < max(stock.price_as_of for stock in stocks):
                raise ValueError("Portfolio context predates the signal session")
        if getattr(self.strategy, "requires_context", False) is True:
            if context is None:
                raise ValueError("This strategy requires portfolio context; missing is not an empty account")
            targets = tuple(self.strategy.select_with_context(stocks, context))
        else:
            targets = tuple(self.strategy.select(stocks))
        if len({target.asset_id for target in targets}) != len(targets):
            raise ValueError("Duplicate target identifiers")
        for target in targets:
            if (target.asset_id not in ids or not isfinite(target.weight)
                    or not isfinite(target.score)
                    or not 0 < target.weight <= self.max_weight + 1e-12):
                raise ValueError("Invalid portfolio target")
        if sum(target.weight for target in targets) > self.max_gross + 1e-12:
            raise ValueError("Gross exposure exceeds limit")
        return targets

# ===== framework/decisions.py =====
"""Identical decision records for QC and local replay; no broker or data SDK."""
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from math import isfinite


def canonical(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not isfinite(value):
        return {"nonfinite": str(value)}
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    decision_id: str
    targets: tuple
    record: dict


def decide(engine: PortfolioEngine, stocks, execution_day, strategy_spec,
           *, input_schema="us_technology_daily_v1", context=None, current_information=False, execution_context=None):
    stocks = tuple(sorted(stocks, key=lambda s: s.asset_id))
    if len({s.price_as_of for s in stocks}) > 1:
        raise ValueError("All candidates must share the same completed signal session")
    targets = engine.plan(stocks, execution_day, context, current_information=current_information)
    record = {
        "schema": 1, "input_schema": input_schema,
        "execution_day": execution_day.isoformat(),
        "strategy": canonical(strategy_spec),
        "limits": {"max_gross": engine.max_gross, "max_weight": engine.max_weight},
        "data_sha256": digest([asdict(s) for s in stocks]),
        "targets": [asdict(t) for t in targets],
    }
    if context is not None:
        record["portfolio_context"] = canonical(asdict(context))
    if current_information is True:
        record['information_mode'] = 'current_observation'
    if execution_context is not None:
        record["execution_context"] = ExecutionContext.from_record(execution_context).record()
    return Decision(digest(record), targets, record)

# ===== framework/collection.py =====
"""Optional, pure strategy hooks for exact ranked-prefix data collection.

Providers supply histories and hydrate candidates in priority order. Only the
strategy decides eligibility, priority and when lower-ranked candidates cannot
change its selection. No vendor, network, account or order handles belong here.
"""
from dataclasses import dataclass, field
from datetime import date
import re
from typing import Callable


@dataclass(frozen=True)
class PriceHistory:
    ticker: str
    price_as_of: date
    adjusted_closes: tuple[float, ...]
    raw_close: float
    share_volumes: tuple[float, ...]
    dollar_volumes: tuple[float, ...] = ()


@dataclass(frozen=True)
class RankedCollectionPolicy:
    signature: str
    priority: Callable = field(repr=False, compare=False)
    complete: Callable = field(repr=False, compare=False)

    def __post_init__(self):
        if (not isinstance(self.signature, str) or not re.fullmatch(r'[0-9a-f]{64}', self.signature)
                or not callable(self.priority) or not callable(self.complete)):
            raise ValueError('Invalid ranked collection policy')

    def record(self):
        return {'kind': 'ranked_prefix_v1', 'signature': self.signature}

# ===== framework/scheduling.py =====
"""Schedules over explicit module-supplied sessions in any IANA timezone.

Date-only calendars are a legacy US-equity compatibility input. New modules
provide each session's actual intervals, including breaks and early closes.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LEGACY_US_SCHEDULE = dict(cadence='monthly', day=1, weekday=0, prepare_time='06:00', trade_time='09:35',
                        window_minutes=60, timezone='America/New_York', poll_seconds=30, initial_entry='immediate')
DEFAULT_SCHEDULE = LEGACY_US_SCHEDULE  # Existing saved plans retain their original interpretation.


@dataclass(frozen=True)
class TradingSession:
    day: date
    intervals: tuple[tuple[datetime, datetime], ...] = ()

    def record(self):
        if not self.intervals:
            return self.day.isoformat()
        return {'date': self.day.isoformat(), 'intervals': [
            {'open': opening.isoformat(), 'close': close.isoformat()} for opening, close in self.intervals]}


def normalize_sessions(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError('Calendar sessions must be a list')
    result = []
    for value in values:
        if isinstance(value, TradingSession):
            item = value
        elif isinstance(value, date) and not isinstance(value, datetime):
            item = TradingSession(value)
        elif isinstance(value, str):
            item = TradingSession(date.fromisoformat(value))
        elif isinstance(value, dict) and set(value) == {'date', 'intervals'}:
            day = date.fromisoformat(value['date'])
            intervals = []
            if not isinstance(value['intervals'], (list, tuple)) or not value['intervals']:
                raise ValueError('Explicit sessions require trading intervals')
            for interval in value['intervals']:
                if not isinstance(interval, dict) or set(interval) != {'open', 'close'}:
                    raise ValueError('Invalid session interval')
                opening, close = (datetime.fromisoformat(interval[k]) for k in ('open', 'close'))
                if opening.tzinfo is None or close.tzinfo is None or close <= opening:
                    raise ValueError('Session intervals require increasing aware timestamps')
                if intervals and opening < intervals[-1][1]:
                    raise ValueError('Trading intervals overlap or are out of order')
                intervals.append((opening, close))
            item = TradingSession(day, tuple(intervals))
        else:
            raise ValueError('Invalid calendar session')
        result.append(item)
    if len({x.day for x in result}) != len(result):
        raise ValueError('Duplicate calendar session dates')
    # Sessions also may not overlap across business dates (overnight is valid).
    actual = sorted((op, cl) for item in result for op, cl in item.intervals)
    if any(actual[i][0] < actual[i-1][1] for i in range(1, len(actual))):
        raise ValueError('Calendar sessions overlap')
    return tuple(sorted(result, key=lambda x: x.day))


def session_dates(values):
    return tuple(x.day for x in normalize_sessions(values))


def schedule_defaults(context=None):
    if context is None:
        return dict(LEGACY_US_SCHEDULE)
    market = ExecutionContext.from_record(context)
    defaults = dict(LEGACY_US_SCHEDULE, timezone=market.timezone)
    if market.market != "US" or market.asset_class != "equity":
        defaults.update(prepare_time="00:00", trade_time="00:00")
    return defaults


def schedule_config(value, *, defaults=None):
    if not isinstance(value, dict) or set(value) - DEFAULT_SCHEDULE.keys():
        raise ValueError('Unknown schedule fields')
    cfg = dict(DEFAULT_SCHEDULE if defaults is None else defaults, **value)
    if cfg['initial_entry'] not in {'immediate', 'scheduled'}:
        raise ValueError('Invalid initial entry rule')
    if cfg['cadence'] not in {'daily', 'weekly', 'monthly'}:
        raise ValueError('Unsupported schedule cadence')
    try:
        if not isinstance(cfg['timezone'], str):
            raise ValueError()
        ZoneInfo(cfg['timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Schedule timezone must be a valid IANA timezone') from None
    for key, low, high in [('day', 1, 28), ('weekday', 0, 6), ('window_minutes', 1, 1440), ('poll_seconds', 15, 300)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid schedule number')
    for key in ('prepare_time', 'trade_time'):
        text = cfg[key]
        if not isinstance(text, str) or len(text) != 5 or text[2] != ':':
            raise ValueError('Time must be HH:MM')
        t = time.fromisoformat(text)
        if t.isoformat(timespec='minutes') != text:
            raise ValueError('Time must be HH:MM')
    if cfg['prepare_time'] > cfg['trade_time']:
        raise ValueError('Preparation time must not follow execution time on the scheduled day')
    return cfg


def instant(day, hhmm, cfg):
    return datetime.combine(day, time.fromisoformat(hhmm), ZoneInfo(cfg['timezone']))


def session_intervals(session, cfg):
    if session.intervals:
        return session.intervals
    if cfg['timezone'] != LEGACY_US_SCHEDULE['timezone']:
        raise ValueError('This market module must provide explicit session intervals')
    return ((instant(session.day, '09:35', cfg), instant(session.day, '16:00', cfg)),)


def session_close(day, sessions, cfg):
    session = next((s for s in normalize_sessions(sessions) if s.day == day), None)
    if session is None:
        raise ValueError('Calendar does not contain this trading session')
    return session_intervals(session, cfg)[-1][1]


def _window_for_day(day, sessions, cfg):
    session = next((s for s in normalize_sessions(sessions) if s.day == day), None)
    if session is None:
        raise ValueError('No exchange session in this schedule period')
    scheduled = instant(day, cfg['trade_time'], cfg)
    for opening, close in session_intervals(session, cfg):
        start = max(scheduled, opening)
        if start < close:
            return start, min(start + timedelta(minutes=cfg['window_minutes']), close)
    raise ValueError('Configured execution time is outside this market session')


def month_window(day, sessions, cfg):
    anchor = day.replace(day=cfg['day'])
    eligible = sorted(s for s in session_dates(sessions) if s.year == day.year and s.month == day.month and s >= anchor)
    if not eligible:
        raise ValueError('Exchange calendar does not cover the monthly anchor')
    return _window_for_day(eligible[0], sessions, cfg)


def period_key(day, cfg):
    if cfg['cadence'] == 'monthly':
        return day.strftime('%Y-%m')
    if cfg['cadence'] == 'weekly':
        return 'week-' + str(day - timedelta(days=day.weekday()))
    return str(day)


def schedule_window(day, sessions, cfg):
    if cfg['cadence'] == 'monthly':
        return month_window(day, sessions, cfg)
    days = session_dates(sessions)
    if cfg['cadence'] == 'weekly':
        monday = day - timedelta(days=day.weekday())
        anchor = monday + timedelta(days=cfg['weekday'])
        eligible = sorted(s for s in days if anchor <= s < monday + timedelta(days=14))
    else:
        eligible = [day] if day in days else []
    if not eligible:
        raise ValueError('No exchange session in this schedule period')
    return _window_for_day(eligible[0], sessions, cfg)


def period_window(day, sessions, cfg):
    if cfg['cadence'] == 'weekly':
        previous = day - timedelta(days=day.weekday() + 7)
        try:
            start, end = schedule_window(previous, sessions, cfg)
            if start.astimezone(ZoneInfo(cfg['timezone'])).date() == day:
                return start, end, period_key(previous, cfg)
        except ValueError:
            pass
    start, end = schedule_window(day, sessions, cfg)
    return start, end, period_key(day, cfg)


def initial_window(now, sessions, cfg):
    """Start at a usable broker-declared interval; never run through a market break."""
    local = now.astimezone(ZoneInfo(cfg['timezone']))
    if cfg.get('initial_entry', 'immediate') == 'scheduled':
        start = next_window(now, sessions, cfg)
        if start is None:
            raise ValueError('Exchange calendar does not cover initial schedule')
        for session in normalize_sessions(sessions):
            for opening, close in session_intervals(session, cfg):
                if opening <= start < close:
                    return max(start, local), min(start + timedelta(minutes=cfg['window_minutes']), close)
        raise ValueError('Scheduled execution is outside supplied sessions')
    for session in normalize_sessions(sessions):
        for opening, close in session_intervals(session, cfg):
            start = max(opening, local)
            if start >= close - timedelta(minutes=5):
                continue
            return start, min(start + timedelta(minutes=cfg['window_minutes']), close)
    raise ValueError('Exchange calendar does not cover the initial portfolio')


def selection_period(day, sessions, cfg, *, first=False):
    start, _, key = period_window(day, sessions, cfg)
    if day not in session_dates(sessions):
        return None
    if first and cfg.get('initial_entry', 'immediate') == 'immediate':
        return key
    return key if start.astimezone(ZoneInfo(cfg['timezone'])).date() == day else None


def next_window(now, sessions, cfg):
    local = now.astimezone(ZoneInfo(cfg['timezone']))
    cursor = local.date() - timedelta(days=7) if cfg['cadence'] == 'weekly' else local.date()
    for _ in range(100):
        try:
            start, end = schedule_window(cursor, sessions, cfg)
            if end > local:
                return start
        except ValueError:
            pass
        if cfg['cadence'] == 'monthly':
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        elif cfg['cadence'] == 'weekly':
            cursor = cursor - timedelta(days=cursor.weekday()) + timedelta(days=7)
        else:
            cursor += timedelta(days=1)
    return None

# ===== strategies/tech_profit_momentum.py =====
"""Tech profit momentum v0.1. No platform calls and no order submission."""

from collections import Counter
from math import isfinite


class TechProfitMomentum:
    def __init__(self, count=5, weight=0.19, lookback=252, skip=21,
                 min_dollar_volume=10_000_000, require_profit=True,
                 industry_limit=2):
        if type(require_profit) is not bool:
            raise ValueError("require_profit must be a JSON boolean, not a string")
        if type(count) is not int or count < 1:
            raise ValueError("count must be a positive integer")
        if (type(lookback) is not int or type(skip) is not int
                or not 0 <= skip < lookback):
            raise ValueError("Expected integer 0 <= skip < lookback")
        if not isfinite(weight) or not 0 < weight <= 1 / count:
            raise ValueError("Invalid position weight")
        if not isfinite(min_dollar_volume) or min_dollar_volume <= 0:
            raise ValueError("Invalid liquidity threshold")
        if type(industry_limit) is not int or industry_limit < 1:
            raise ValueError("industry_limit must be positive")
        self.count, self.weight = count, weight
        self.lookback, self.skip = lookback, skip
        self.min_dollar_volume = min_dollar_volume
        self.require_profit, self.industry_limit = require_profit, industry_limit

    @property
    def history_requirements(self):
        return HistoryRequirements(self.lookback + 1, 20)

    def select(self, stocks):
        ranked = []
        for stock in stocks:
            prices, volumes = stock.adjusted_closes, stock.dollar_volumes
            if (len(prices) < self.lookback + 1 or len(volumes) < 20
                    or not stock.industry
                    or any(not isfinite(p) or p <= 0 for p in prices)
                    or any(not isfinite(v) or v <= 0 for v in volumes[-20:])):
                continue
            if self.require_profit and (not isfinite(stock.operating_income_ttm)
                                        or stock.operating_income_ttm <= 0):
                continue
            if sum(volumes[-20:]) / 20 < self.min_dollar_volume:
                continue
            score = prices[-1 - self.skip] / prices[-1 - self.lookback] - 1
            if isfinite(score):
                ranked.append((score, stock))
        # Stable SID tie-breaker; never choose based on today's surviving tickers.
        ranked.sort(key=lambda item: (-item[0], item[1].asset_id))
        selected, industries = [], Counter()
        for score, stock in ranked:
            if industries[stock.industry] >= self.industry_limit:
                continue
            selected.append(StockTarget(stock.asset_id, self.weight, score))
            industries[stock.industry] += 1
            if len(selected) == self.count:
                break
        # Unfilled slots remain cash. Negative momentum does not trigger cash.
        return tuple(selected)

# Frozen plug-in registry generated from local manifests.
_RUN_CONFIG = {'schema_version': 1, 'strategy': {'type': 'tech_profit_momentum', 'count': 5, 'weight': 0.19, 'lookback': 252, 'skip': 21, 'min_dollar_volume': 10000000, 'require_profit': True, 'industry_limit': 2}, 'initial_cash': '100000', 'limits': {'max_gross': '0.95', 'max_weight': '0.19', 'max_order_value': '100000', 'max_quote_age_seconds': 120}, 'fees': {'per_order': '1', 'bps': '0'}, 'backtest': {'backend': 'quantconnect', 'start': '2020-01-01', 'end': '2020-12-31', 'slippage_bps': 10}, 'schedule': {'cadence': 'monthly', 'day': 1, 'weekday': 0, 'prepare_time': '06:00', 'trade_time': '09:35', 'window_minutes': 60, 'timezone': 'America/New_York', 'poll_seconds': 30, 'initial_entry': 'scheduled'}, 'benchmark': 'QQQ'}
_INPUT_SCHEMA = 'us_technology_daily_v1'
_QC_PROFILE = {}
_PLUGIN_CLASSES = {'tech_profit_momentum': TechProfitMomentum}
_PLUGIN_SPECS = {'tech_profit_momentum': {'type': 'tech_profit_momentum', 'version': '0.1', 'source_sha256': '530c3c3a0b388da1006ac8f54439aa71e3807578306599e581e8672cc097d956', 'engine_sha256': '48c1d57552324bb427e51167c141de2a99a3c0544513fda61cb401256f36281c', 'parameters': {'count': 5, 'weight': 0.19, 'lookback': 252, 'skip': 21, 'min_dollar_volume': 10000000, 'require_profit': True, 'industry_limit': 2}}}
def build_portfolio_strategy(config):
    return _PLUGIN_CLASSES[config['type']](**{k:v for k,v in config.items() if k not in {'type','id'}})
def portfolio_spec(config):
    base = _PLUGIN_SPECS[config['type']]
    params = dict(base['parameters'])
    params.update({k:v for k,v in config.items() if k not in {'type','id'}})
    return dict(base, parameters=params)

# ===== backtests/quantconnect/adapter.py =====
"""QuantConnect adapter; bundle with tools/build_quantconnect.py before upload."""

from AlgorithmImports import *
from datetime import datetime, timedelta
import json
from math import isfinite


# The exporter freezes the reviewed config here; local tests load the same config.
if "_RUN_CONFIG" not in globals():
    _RUN_CONFIG = {k: v for k, v in default_portfolio_config().items()
                   if k not in {"data", "broker"}}
    _entry = strategy_catalog()[_RUN_CONFIG['strategy']['type']]
    _INPUT_SCHEMA = _entry['input_schema']
    _QC_PROFILE = _entry.get('backtest', {}).get('quantconnect', {})
START = tuple(map(int, _RUN_CONFIG["backtest"]["start"].split("-")))
END = tuple(map(int, _RUN_CONFIG["backtest"]["end"].split("-")))
INITIAL_CASH = float(_RUN_CONFIG["initial_cash"])
SLIPPAGE_BPS = float(_RUN_CONFIG["backtest"]["slippage_bps"])
FEE_PER_ORDER = float(_RUN_CONFIG["fees"]["per_order"])
MODE = "configured"  # Legacy momentum/profit/full overrides remain explicit opt-ins.
VERSION = "0.4.0"
RUN_SCHEDULE = schedule_config(_RUN_CONFIG.get('schedule', {'initial_entry': 'scheduled'}))
STRATEGY_TYPE = _RUN_CONFIG["strategy"]["type"]
STRATEGY_PARAMETERS = {k: v for k, v in _RUN_CONFIG["strategy"].items() if k not in {"type", "id"}}


def strategy_configuration():
    # When the exported type is switched manually, do not pass another plug-in's parameters.
    params = dict(STRATEGY_PARAMETERS) if STRATEGY_TYPE == _RUN_CONFIG["strategy"]["type"] else {}
    if STRATEGY_TYPE == "tech_profit_momentum" and MODE != "configured":
        params.update(require_profit=MODE != "momentum",
                      industry_limit=2 if MODE == "full" else params.get("count", 5))
    return {"type": STRATEGY_TYPE, **params}


def make_decision(engine, candidates, execution_day, context=None):
    return decide(engine, candidates, execution_day, portfolio_spec(strategy_configuration()),
                  input_schema=_INPUT_SCHEMA, context=context)


class TechProfitMomentumAlgorithm(QCAlgorithm):
    def initialize(self):
        if self.live_mode:
            raise RuntimeError("Research backtest only; live execution is not implemented")
        if MODE not in ("configured", "momentum", "profit", "full"):
            raise ValueError("Unknown MODE")
        if float(_RUN_CONFIG["fees"].get("bps", 0)) != 0:
            raise ValueError("This QC adapter supports fixed per-order fees only")
        self.set_start_date(*START)
        self.set_end_date(*END)
        self.set_cash(INITIAL_CASH)
        self.set_time_zone(TimeZones.NEW_YORK)
        self.settings.daily_precise_end_time = True
        self.settings.free_portfolio_value_percentage = 0
        self.universe_settings.resolution = Resolution.MINUTE
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.universe_settings.leverage = 1
        self.universe_settings.fill_forward = False
        self.universe_settings.asynchronous = False
        self.add_security_initializer(self._configure_security)
        self.qqq = self.add_equity(
            _RUN_CONFIG.get('benchmark', 'QQQ'), Resolution.MINUTE,
            data_normalization_mode=DataNormalizationMode.ADJUSTED
        ).symbol
        self.set_benchmark(self.qqq)
        self._hours = self.securities[self.qqq].exchange.hours
        self._engine = PortfolioEngine(build_portfolio_strategy(strategy_configuration()),
            float(_RUN_CONFIG["limits"]["max_gross"]), float(_RUN_CONFIG["limits"]["max_weight"]))
        self._fact_snapshots = {}
        self._last_period = None
        self._pending = None
        self._selected_symbols = []
        self._completed = 0
        self.add_universe(self._select_universe)
        # Only to obtain prior fundamental snapshots, not to trade.
        # Each scheduled signal requests the strategy's declared price history.
        self.set_warm_up(timedelta(days=10), Resolution.DAILY)

    def _configure_security(self, security):
        security.set_leverage(1)
        security.set_fee_model(ConstantFeeModel(FEE_PER_ORDER, "USD"))
        security.set_slippage_model(ConstantSlippageModel(SLIPPAGE_BPS / 10_000))

    def _freeze_facts(self, fundamentals):
        facts = {}
        for f in fundamentals:
            all_equities = _QC_PROFILE.get('universe') == 'us_common_stock'
            if (not f.has_fundamental_data or (not all_equities
                    and f.asset_classification.morningstar_sector_code != MorningstarSectorCode.TECHNOLOGY)):
                continue
            ref = f.security_reference
            if (ref.security_type != "ST00000001" or ref.is_depositary_receipt
                    or not ref.is_primary_share
                    or (all_equities and f.company_reference.primary_exchange_id not in {'NYS', 'NAS', 'ASE'})):
                continue
            # Copy scalar values now. Fundamental objects have time-dependent getters.
            income = (0.0 if all_equities else
                      float(f.financial_statements.income_statement.operating_income.twelve_months))
            industry = int(f.asset_classification.morningstar_industry_group_code if all_equities
                           else f.asset_classification.morningstar_industry_code)
            if industry <= 0:
                continue
            sid = str(f.symbol.id)
            if all_equities:
                cap = float(f.market_cap)
                if not isfinite(cap) or cap <= 0:
                    continue
                facts[sid] = (f.symbol, str(industry), income, cap)
            else:
                facts[sid] = (f.symbol, str(industry), income)
        return facts

    def _select_universe(self, fundamentals):
        # Data arrivals need not occur on an exchange session. In particular,
        # Friday's data may arrive on Saturday. Never discard those snapshots.
        self._fact_snapshots[self.time] = self._freeze_facts(fundamentals)
        oldest = self.time - timedelta(days=14)
        self._fact_snapshots = {stamp: facts for stamp, facts in self._fact_snapshots.items()
                                if stamp >= oldest}
        # A weekend callback can prepare Monday's basket. Do not depend on a
        # callback arriving on the first trading morning itself.
        execution_day = self.time.date()
        while not self._is_session(execution_day):
            execution_day += timedelta(days=1)
        # Preparing a basket at the warmup boundary is safe; on_data still
        # forbids orders during warmup. Otherwise a holiday start can miss January.
        if execution_day < datetime(*START).date():
            return Universe.UNCHANGED
        sessions = [execution_day + timedelta(days=i) for i in range(-35, 40)
                    if self._is_session(execution_day + timedelta(days=i))]
        period = selection_period(execution_day, sessions, RUN_SCHEDULE, first=self._last_period is None)
        if period is None or period == self._last_period:
            return Universe.UNCHANGED
        signal_day = self._previous_session(execution_day)
        snapshot_at, prior_facts = self._facts_for_signal(signal_day)
        candidates = self._load_candidates(prior_facts, signal_day, snapshot_at.date())
        context = None
        if getattr(self._engine.strategy, "requires_context", False) is True:
            context = PortfolioContext(self.time.date(), tuple(
                str(kv.key.id) for kv in self.portfolio if kv.value.invested))
        decision = make_decision(self._engine, candidates, execution_day, context)
        targets = decision.targets
        by_id = {stock.asset_id: stock for stock in candidates}
        symbols = {sid: facts[0] for sid, facts in prior_facts.items()}
        self._selected_symbols = [symbols[t.asset_id] for t in targets]
        self._pending = (execution_day, targets, symbols)
        self._last_period = period
        audit = {
            "version": VERSION, "execution_day": str(execution_day),
            "decision_id": decision.decision_id, "strategy": decision.record["strategy"],
            "signal_day": str(signal_day), "fundamentals_known_at": snapshot_at.isoformat(),
            "mode": MODE, "complete_histories": len(candidates),
            "selected": [{"ticker": by_id[t.asset_id].ticker, "weight": t.weight,
                          "momentum": round(t.score, 6),
                          "industry": by_id[t.asset_id].industry,
                          "operating_income_ttm": (by_id[t.asset_id].operating_income_ttm
                              if isfinite(by_id[t.asset_id].operating_income_ttm) else None)}
                         for t in targets]
        }
        self.log("SELECTION " + json.dumps(audit, allow_nan=False))
        return self._selected_symbols

    def _is_session(self, day):
        return self._hours.is_date_open(datetime.combine(day, datetime.min.time()))

    def _previous_session(self, day):
        day -= timedelta(days=1)
        while not self._is_session(day):
            day -= timedelta(days=1)
        return day

    def _facts_for_signal(self, signal_day):
        # Preserve the original conservative policy: only information received
        # by midnight at the start of the signal session. For a Monday
        # signal, Saturday's delivery is valid; a Monday callback is optional.
        cutoff = datetime.combine(signal_day, datetime.min.time())
        earliest = datetime.combine(self._previous_session(signal_day) + timedelta(days=1),
                                    datetime.min.time())
        eligible = [stamp for stamp in self._fact_snapshots if earliest <= stamp <= cutoff]
        if not eligible:
            cached = ", ".join(stamp.isoformat() for stamp in sorted(self._fact_snapshots))
            raise RuntimeError(f"Missing timely fundamentals: required {earliest} through {cutoff}; cached [{cached}]")
        stamp = max(eligible)
        facts = self._fact_snapshots[stamp]
        if not facts:
            raise RuntimeError(f"Empty equity fundamental snapshot at {stamp}; check data coverage")
        return stamp, facts

    @staticmethod
    def _daily_rows(history, symbol, cutoff):
        if history.empty:
            return {}
        try:
            frame = history.loc[symbol].sort_index()
        except KeyError:
            return {}
        rows = {}
        for stamp, row in frame.iterrows():
            end = stamp.to_pydatetime()
            if end > cutoff:
                raise ValueError("Future history bar")
            # Supports both precise daily close stamps and midnight end stamps.
            day = (end - timedelta(microseconds=1)).date() if end.hour == end.minute == 0 else end.date()
            if day in rows:
                raise ValueError("Duplicate daily bar")
            rows[day] = (float(row["close"]), float(row["volume"]))
        return rows

    def _history(self, symbols, count, normalization):
        return self.history(
            symbols, count, Resolution.DAILY, fill_forward=False,
            extended_market_hours=False, data_normalization_mode=normalization
        )

    def _load_candidates(self, facts, prior_day, known_on=None):
        requirements = build_portfolio_strategy(strategy_configuration()).history_requirements
        history_count = max(requirements.price_bars, requirements.dollar_volume_bars)
        reference = self._daily_rows(
            self._history([self.qqq], history_count, DataNormalizationMode.ADJUSTED), self.qqq, self.time
        )
        sessions = sorted(reference)
        if len(sessions) != history_count or sessions[-1] != prior_day:
            raise RuntimeError(f"Expected {history_count} completed daily bars through previous session")
        # Selection rules (including profitability) belong to the strategy.
        # All point-in-time candidates use the selected input data contract.
        pool = sorted(facts.items())
        result = []
        for offset in range(0, len(pool), 40):
            batch = pool[offset:offset + 40]
            symbols = [f[0] for _, f in batch]
            adjusted = self._history(symbols, requirements.price_bars, DataNormalizationMode.ADJUSTED)
            raw = self._history(symbols, requirements.dollar_volume_bars, DataNormalizationMode.RAW)
            equity_input = _INPUT_SCHEMA == 'us_equity_momentum_daily_v1'
            split_history = (self._history(symbols, requirements.dollar_volume_bars,
                             DataNormalizationMode.SPLIT_ADJUSTED) if equity_input else None)
            for sid, fact in batch:
                symbol, industry, income = fact[:3]
                prices = self._daily_rows(adjusted, symbol, self.time)
                volumes = self._daily_rows(raw, symbol, self.time)
                # An IPO, halt or missing session cannot shorten the lookback silently.
                price_days = sessions[-requirements.price_bars:]
                volume_days = sessions[-requirements.dollar_volume_bars:]
                if not all(day in prices for day in price_days) or not all(day in volumes for day in volume_days):
                    continue
                values = (
                    sid, symbol.value, known_on or prior_day, sessions[-1], industry, income,
                    tuple(prices[day][0] for day in price_days),
                    tuple(volumes[day][0] * volumes[day][1] for day in volume_days)
                )
                if equity_input:
                    split_rows = self._daily_rows(split_history, symbol, self.time)
                    if not all(day in split_rows for day in volume_days):
                        continue
                    # SplitAdjusted historical volume uses the provider's whole
                    # history scale. Bring it back to the signal-session share
                    # units so a later split cannot change the 1m-share filter.
                    split_scale = split_rows[prior_day][0] / volumes[prior_day][0]
                    result.append(EquitySnapshot(*values, fact[3], volumes[prior_day][0],
                        tuple(split_rows[day][1] * split_scale for day in volume_days)))
                else:
                    result.append(StockSnapshot(*values))
        return result

    def _fresh(self, data, symbol):
        if not data.bars.contains_key(symbol):
            return False
        bar = data.bars[symbol]
        return (not bar.is_fill_forward and bar.end_time == self.time
                and bar.close > 0 and self.securities[symbol].is_tradable)

    def on_data(self, data):
        if self.is_warming_up or self._pending is None:
            return
        today, targets, symbols = self._pending
        if self.time.date() < today:
            return
        if self.time.date() > today:
            raise RuntimeError("Rebalance missed its execution session")
        trade_time = tuple(map(int, RUN_SCHEDULE['trade_time'].split(':')))
        if self._completed == 0 and RUN_SCHEDULE['initial_entry'] == 'immediate':
            trade_time = (9, 35)
        if (self.time.hour, self.time.minute) < trade_time:
            return
        window_end = datetime.combine(today, datetime.min.time()) + timedelta(
            hours=trade_time[0], minutes=trade_time[1] + RUN_SCHEDULE['window_minutes'])
        if self.time >= window_end:
            raise RuntimeError("Rebalance missed its execution window")
        held = [kv.key for kv in self.portfolio if kv.value.invested]
        missing_held = [s for s in held if not self._fresh(data, s)]
        missing_new = [symbols[t.asset_id] for t in targets if not self._fresh(data, symbols[t.asset_id])]
        if missing_held or missing_new:
            deadline = datetime.combine(today, datetime.min.time()) + timedelta(
                hours=trade_time[0], minutes=trade_time[1] + min(25, RUN_SCHEDULE['window_minutes'] - 1))
            if self.time < deadline:
                return
            if missing_held:
                raise RuntimeError("Cannot safely rebalance stale or halted existing holdings")
            self.log("UNFILLED cash slots: " + ",".join(s.value for s in missing_new))
        executable = [PortfolioTarget(symbols[t.asset_id], t.weight) for t in targets
                      if self._fresh(data, symbols[t.asset_id])]
        self._pending = None
        tag = STRATEGY_TYPE + " " + str(today)
        if executable:
            # LEAN processes reductions before increases, including reweighting
            # retained names. Synchronous market orders; RAW portfolio accounting.
            self.set_holdings(executable, liquidate_existing_holdings=True, tag=tag)
        else:
            self.liquidate(tag=tag)
        self._completed += 1
        self.plot("Portfolio", "Target stock count", len(executable))
        self.log("REBALANCED " + str(today) + " targets=" + str(len(executable)))

    def on_order_event(self, event):
        if event.status == OrderStatus.INVALID:
            raise RuntimeError("Rejected backtest order: " + str(event))

    def on_end_of_algorithm(self):
        self.set_runtime_statistic("Version", VERSION)
        # Encoded hex values can be omitted from QC runtime statistics. Keep
        # the fingerprint in logs and human-readable run settings in the banner.
        self.log("RESEARCH_CONFIG " + json.dumps({"sha256": digest(_RUN_CONFIG), "configuration": _RUN_CONFIG}))
        self.set_runtime_statistic("Research start", datetime(*START).date().isoformat())
        self.set_runtime_statistic("Research end", datetime(*END).date().isoformat())
        self.set_runtime_statistic("Rebalances", str(self._completed))
        self.set_runtime_statistic("Schedule", RUN_SCHEDULE['cadence'] + ' ' + RUN_SCHEDULE['trade_time'])
        self.set_runtime_statistic("First entry", RUN_SCHEDULE['initial_entry'])
        self.set_runtime_statistic("Strategy mode", MODE)
        self.set_runtime_statistic("Costs", f"${FEE_PER_ORDER}/order + {SLIPPAGE_BPS}bps slippage")
