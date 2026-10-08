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
