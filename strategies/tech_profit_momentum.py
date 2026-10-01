"""Tech profit momentum v0.1. No platform calls and no order submission."""

from collections import Counter
from math import isfinite
from framework.portfolio import HistoryRequirements, StockTarget


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
