"""Generic single-asset research engine; imports no strategies or platforms."""
import math
from datetime import date
from .contracts import Strategy


def backtest(rows, strategy: Strategy, period, capital, weight, cost_bps, minimum=200):
    if not all(math.isfinite(v) for v in (capital, weight, cost_bps)) or not (
        capital > 0 and 0 < weight <= 1 and 0 <= cost_bps < 10000
    ):
        raise ValueError("Invalid research portfolio settings")
    start, stop = map(date.fromisoformat, period)
    indices = [i for i, (d, _) in enumerate(rows) if start <= d <= stop]
    if len(indices) < minimum:
        raise ValueError("Insufficient evaluation sessions")
    first, last = indices[0], indices[-1]
    if (rows[first][0] - start).days > 7 or (stop - rows[last][0]).days > 7:
        raise ValueError("Data does not cover requested period boundaries")
    if first < max(1, strategy.warmup):
        raise ValueError("Data must include pre-period warmup")
    prices = [p for _, p in rows]
    cash, asset = float(capital), 0.0
    peak, drawdown, turnover, fees, trades = capital, 0.0, 0.0, 0.0, 0
    curve = []
    previous_target = None
    for i in indices:
        asset *= prices[i] / prices[i-1]
        equity = cash + asset
        raw_target = strategy.target(tuple(rows[:i]))
        if isinstance(raw_target, bool) or not math.isfinite(raw_target) or not 0 <= raw_target <= 1:
            raise ValueError("Strategy target must be a finite exposure in [0,1]")
        target = weight * raw_target
        # Rebalance only when the target signal changes, not daily drift.
        if target != previous_target:
            rate = cost_bps / 10000
            gap = target * equity - asset
            delta = gap / (1 + target * rate) if gap >= 0 else gap / (1 - target * rate)
            fee = abs(delta) * rate
            cash -= delta + fee
            asset += delta
            fees += fee
            turnover += abs(delta) / equity
            trades += int(abs(delta) > 1e-8)
            previous_target = target
        equity = cash + asset
        if equity <= 0 or cash < -1e-7:
            raise ValueError("Invalid or leveraged portfolio")
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
        curve.append({"date": rows[i][0].isoformat(), "equity": equity, "target": target})
    years = (rows[last][0] - rows[first][0]).days / 365.25
    if years <= 0:
        raise ValueError("Evaluation requires multiple dates")
    return {"cagr": (equity / capital) ** (1 / years) - 1,
            "max_drawdown": drawdown, "final_equity": equity,
            "fees": fees, "trades": trades, "turnover": turnover,
            "sessions": len(indices), "curve": curve}

