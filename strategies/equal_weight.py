"""A simple control plug-in for testing composition, not an investment thesis."""
from math import isfinite
from framework.portfolio import HistoryRequirements, StockTarget


class EqualWeight:
    def __init__(self, count=5, weight=0.19):
        if type(count) is not int or count < 1 or not isfinite(weight) or not 0 < weight <= 1 / count:
            raise ValueError("Invalid equal-weight settings")
        self.count, self.weight = count, weight

    @property
    def history_requirements(self):
        # Keep the same eligible price-history universe as the baseline control.
        return HistoryRequirements(253, 20)

    def select(self, stocks):
        return tuple(StockTarget(s.asset_id, self.weight, 0.0)
                     for s in sorted(stocks, key=lambda s: s.asset_id)[:self.count])
