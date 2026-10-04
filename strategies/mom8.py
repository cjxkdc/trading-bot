"""MOM8 selection adapted from Yizhi Song's FriesTrader (MIT).

See MOM8.md and THIRD_PARTY_NOTICES.md. The framework deliberately supplies
95%-gross equal-weight rebalancing and cash reserves, not the upstream sizing.
No platform imports, data downloads, scheduling or broker calls belong here.
"""
from collections import Counter
from datetime import date
from math import isfinite

from framework.portfolio import HistoryRequirements, PortfolioContext, StockTarget
from framework.collection import RankedCollectionPolicy


class MOM8:
    requires_context = True
    # A research preview is explicitly a hypothetical initial portfolio. Actual
    # execution and QC always require observed, frozen portfolio membership.
    research_preview_note = '首次建仓示意：假设未持股；实际持仓的前 12 名保留缓冲尚未应用。'

    def __init__(self, count=8, weight=0.11875, lookback=252, skip=21,
                 trend_window=200, keep_buffer=12, industry_limit=2,
                 min_market_cap=2_000_000_000, max_market_cap=500_000_000_000,
                 min_price=5.0, min_average_volume=1_000_000):
        integers = (count, lookback, skip, trend_window, keep_buffer, industry_limit)
        if any(type(v) is not int for v in integers):
            raise ValueError('MOM8 periods and counts must be integers')
        if not (1 <= count <= keep_buffer and 0 <= skip < lookback
                and 2 <= trend_window <= lookback + 1 and 1 <= industry_limit <= count):
            raise ValueError('Invalid MOM8 history, buffer or industry count')
        numbers = (weight, min_market_cap, max_market_cap, min_price, min_average_volume)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v <= 0
               for v in numbers):
            raise ValueError('Invalid MOM8 numeric parameter')
        if weight * count > 1 + 1e-12 or min_market_cap > max_market_cap:
            raise ValueError('Invalid MOM8 exposure or market-cap bounds')
        self.count, self.weight = count, weight
        self.lookback, self.skip, self.trend_window = lookback, skip, trend_window
        self.keep_buffer, self.industry_limit = keep_buffer, industry_limit
        self.min_market_cap, self.max_market_cap = min_market_cap, max_market_cap
        self.min_price, self.min_average_volume = min_price, min_average_volume

    @property
    def history_requirements(self):
        return HistoryRequirements(max(self.lookback + 1, self.trend_window, 20), 20)

    def price_score(self, stock):
        """The price/volume part of rank(), reused before requesting metadata."""
        prices, volumes = stock.adjusted_closes, stock.share_volumes
        if len(prices) < self.history_requirements.price_bars or len(volumes) < 20:
            return None
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v <= 0
                for v in (*prices, stock.raw_close))
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v) or v < 0
                       for v in volumes[-20:])):
            return None
        if (stock.raw_close < self.min_price or sum(volumes[-20:]) / 20 < self.min_average_volume
                or prices[-1] <= sum(prices[-self.trend_window:]) / self.trend_window):
            return None
        score = prices[-1-self.skip] / prices[-1-self.lookback] - 1
        return score if isfinite(score) else None

    def data_collection_policy(self, signature):
        # Once the diversified keep-buffer is filled in descending price-score
        # order, no remaining candidate can change it, including held-stock
        # retention. Do not stop at the initial purchase count of eight.
        return RankedCollectionPolicy(signature, self.price_score,
                                      lambda stocks: len(self.rank(stocks)) == self.keep_buffer)

    def rank(self, stocks):
        ranked = []
        for stock in stocks:
            # Reject a wrong data contract rather than interpreting missing
            # volume/capitalization as an empty basket and liquidating it.
            if not all(hasattr(stock, k) for k in ('market_cap', 'raw_close', 'share_volumes')):
                raise ValueError('MOM8 requires the US equity momentum data schema')
            score = self.price_score(stock)
            cap = stock.market_cap
            if (score is None or isinstance(cap, bool) or not isinstance(cap, (int, float)) or not isfinite(cap)
                    or not stock.industry or not self.min_market_cap <= cap <= self.max_market_cap):
                continue
            ranked.append((score, stock))
        # Same ticker tie-break as the deployed raw/SIC upstream MOM8 policy.
        ranked.sort(key=lambda item: (-item[0], item[1].ticker, item[1].asset_id))
        counts, diversified = Counter(), []
        for score, stock in ranked:
            if counts[stock.industry] >= self.industry_limit:
                continue
            diversified.append((score, stock))
            counts[stock.industry] += 1
            if len(diversified) == self.keep_buffer:
                break
        return tuple(diversified)

    def select_with_context(self, stocks, context):
        if context is None or not hasattr(context, 'held_asset_ids'):
            raise ValueError('MOM8 requires an explicit portfolio context')
        ranked = self.rank(stocks)
        held = set(context.held_asset_ids)
        keep = [(score, s) for score, s in ranked if s.asset_id in held]
        if len(keep) > self.count:
            raise ValueError('More retained holdings than MOM8 slots; reconcile the strategy account first')
        selected = keep + [(score, s) for score, s in ranked if s.asset_id not in held][:self.count-len(keep)]
        return tuple(StockTarget(s.asset_id, self.weight, score) for score, s in selected)

    def research_targets(self, stocks):
        return self.select_with_context(stocks, PortfolioContext(date.min, ()))
