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
class StockTarget:
    asset_id: str
    weight: float
    score: float


class PortfolioStrategy(Protocol):
    @property
    def history_requirements(self) -> HistoryRequirements: ...

    def select(self, stocks: Sequence[StockSnapshot]) -> tuple[StockTarget, ...]: ...


class PortfolioEngine:
    """Validate information timing and targets without importing any platform."""

    def __init__(self, strategy: PortfolioStrategy, max_gross=0.95, max_weight=0.19):
        if not 0 < max_weight <= max_gross <= 1:
            raise ValueError("Invalid long-only portfolio limits")
        self.strategy = strategy
        self.max_gross, self.max_weight = max_gross, max_weight

    def plan(self, stocks: Sequence[StockSnapshot], execution_day: date):
        stocks = tuple(stocks)
        ids = {stock.asset_id for stock in stocks}
        if len(ids) != len(stocks):
            raise ValueError("Duplicate asset identifiers")
        for stock in stocks:
            if not stock.known_on <= stock.price_as_of < execution_day:
                raise ValueError("Information must precede the execution session")
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
