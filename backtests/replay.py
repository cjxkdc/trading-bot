"""Replay the portfolio pipeline over a series of saved snapshots.

A snapshot is the exact input a live run sees (universe, histories, fundamentals
as known on its date). Replaying the saved series answers "what would this
strategy have done on the days I actually had data for"; it is not a
survivorship-free, tick-level backtest. Fills are assumed at the snapshot's
last adjusted close plus slippage; whole shares only; no platform imports.
"""
from datetime import date, timedelta
from math import floor, isfinite, sqrt

from framework.portfolio import PortfolioContext, PortfolioEngine

PROVIDER = 'local_replay'
SCHEMA_VERSION = 1


def period_key(day, cadence):
    if cadence == 'daily':
        return day.isoformat()
    if cadence == 'weekly':
        year, week, _ = day.isocalendar()
        return f'{year}-W{week:02d}'
    if cadence == 'monthly':
        return f'{day.year}-{day.month:02d}'
    raise ValueError('Unsupported replay cadence')


def rebalance_days(days, cadence):
    """The first saved snapshot of each period is the rebalance session."""
    chosen, seen = [], set()
    for day in sorted(days):
        key = period_key(day, cadence)
        if key not in seen:
            seen.add(key)
            chosen.append(day)
    return chosen


def replay(series, strategy, *, max_gross, max_weight, initial_cash, fee_per_order, fee_bps,
           slippage_bps, cadence='monthly', start=None, end=None):
    """Run the strategy over dated snapshots and value the portfolio at each one.

    series: iterable of (date, stocks) with stocks as framework StockSnapshot
    tuples; dates must be unique. Positions carry over between snapshots; a
    held asset missing from a later snapshot keeps its last price (flagged).
    """
    items = sorted(((day, tuple(stocks)) for day, stocks in series), key=lambda item: item[0])
    if len({day for day, _ in items}) != len(items):
        raise ValueError('Replay dates must be unique')
    if start is not None:
        items = [item for item in items if item[0] >= start]
    if end is not None:
        items = [item for item in items if item[0] <= end]
    if len(items) < 2:
        raise ValueError('At least two dated snapshots are needed to replay')
    for value in (initial_cash, fee_per_order, fee_bps, slippage_bps):
        if not isfinite(float(value)) or float(value) < 0:
            raise ValueError('Replay costs and capital must be non-negative numbers')
    if float(initial_cash) <= 0:
        raise ValueError('Replay needs positive initial cash')
    engine = PortfolioEngine(strategy, float(max_gross), float(max_weight))
    rebalances = set(rebalance_days([day for day, _ in items], cadence))
    cash, shares, last_price = float(initial_cash), {}, {}
    warnings, missing_flagged = [], set()
    nav_points, events, total_orders = [], [], 0
    slip = float(slippage_bps) / 10_000

    def mark(day, stocks):
        prices = {stock.asset_id: stock.adjusted_closes[-1] for stock in stocks if stock.adjusted_closes}
        for asset_id in shares:
            if asset_id in prices:
                last_price[asset_id] = prices[asset_id]
            elif asset_id not in missing_flagged:
                missing_flagged.add(asset_id)
                warnings.append(f'{asset_id} left the universe on {day.isoformat()}; valued at its last known price')
        return cash + sum(quantity * last_price[asset_id] for asset_id, quantity in shares.items())

    for day, stocks in items:
        nav_before = mark(day, stocks)
        orders = 0
        if day in rebalances:
            context = PortfolioContext(day, tuple(sorted(shares))) if getattr(strategy, 'requires_context', False) else None
            targets = engine.plan(stocks, day + timedelta(days=1), context)
            prices = {stock.asset_id: stock.adjusted_closes[-1] for stock in stocks if stock.adjusted_closes}
            wanted = {}
            for target in targets:
                price = prices.get(target.asset_id)
                if price is None or not isfinite(price) or price <= 0:
                    continue
                wanted[target.asset_id] = floor(nav_before * target.weight / price)
            # Sell first (down to the target or out), then buy with the cash that is left.
            for asset_id in sorted(shares):
                desired = wanted.get(asset_id, 0)
                if shares[asset_id] > desired:
                    quantity = shares[asset_id] - desired
                    price = last_price[asset_id] * (1 - slip)
                    proceeds = quantity * price
                    cash += proceeds - float(fee_per_order) - proceeds * float(fee_bps) / 10_000
                    shares[asset_id] = desired
                    orders += 1
            for asset_id in sorted(wanted):
                desired = wanted[asset_id]
                held = shares.get(asset_id, 0)
                if desired > held:
                    price = prices[asset_id] * (1 + slip)
                    quantity = desired - held
                    cost = quantity * price
                    fees = float(fee_per_order) + cost * float(fee_bps) / 10_000
                    if cost + fees > cash:
                        quantity = floor((cash - float(fee_per_order)) / (price * (1 + float(fee_bps) / 10_000)))
                        if quantity <= 0:
                            continue
                        cost = quantity * price
                        fees = float(fee_per_order) + cost * float(fee_bps) / 10_000
                    cash -= cost + fees
                    shares[asset_id] = held + quantity
                    last_price[asset_id] = prices[asset_id]
                    orders += 1
            shares = {asset_id: quantity for asset_id, quantity in shares.items() if quantity > 0}
            total_orders += orders
            events.append({'date': day.isoformat(), 'targets': len(targets), 'orders': orders,
                           'nav_before': round(nav_before, 2), 'cash_after': round(cash, 2),
                           'positions': len(shares)})
        nav_points.append((day, mark(day, stocks)))

    metrics = summarize([value for _, value in nav_points], [day for day, _ in nav_points], total_orders)
    return {'provider': PROVIDER, 'schema_version': SCHEMA_VERSION, 'completion': 'reported_complete',
            'period': {'start': items[0][0].isoformat(), 'end': items[-1][0].isoformat()},
            'cadence': cadence, 'snapshot_count': len(items), 'rebalance_count': len(events),
            'initial_cash': float(initial_cash), 'slippage_bps': float(slippage_bps),
            'fees': {'per_order': float(fee_per_order), 'bps': float(fee_bps)},
            'metrics': metrics, 'nav': [[day.isoformat(), round(value, 2)] for day, value in nav_points],
            'rebalances': events, 'final_positions': dict(sorted(shares.items())),
            'warnings': warnings}


def summarize(values, days, total_orders):
    first, last = values[0], values[-1]
    total_return = last / first - 1 if first else None
    span = (days[-1] - days[0]).days
    annual_return = ((1 + total_return) ** (365.25 / span) - 1) if total_return is not None and span >= 30 and 1 + total_return > 0 else None
    peak, drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, 1 - value / peak)
    returns = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    sharpe = None
    if len(returns) >= 3:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        if variance > 0:
            periods_per_year = 365.25 / (span / len(returns))
            sharpe = mean / sqrt(variance) * sqrt(periods_per_year)
    return {'total_return': total_return, 'annual_return': annual_return, 'max_drawdown': drawdown,
            'sharpe': sharpe, 'total_orders': float(total_orders)}


def parse_replay(payload):
    """Normalize a saved replay result the way QC results are normalized."""
    if not isinstance(payload, dict) or payload.get('provider') != PROVIDER or payload.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('Not a local replay result')
    period = payload.get('period') or {}
    metrics = payload.get('metrics') or {}
    for key in ('start', 'end'):
        date.fromisoformat(str(period[key]))
    if period['start'] > period['end']:
        raise ValueError('Invalid replay period')
    cleaned = {}
    for key in ('total_return', 'annual_return', 'max_drawdown', 'sharpe', 'total_orders'):
        value = metrics.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)):
            raise ValueError('Invalid replay statistic')
        cleaned[key] = None if value is None else float(value)
    if cleaned['total_orders'] is not None and (cleaned['total_orders'] < 0 or not cleaned['total_orders'].is_integer()):
        raise ValueError('Invalid replay order count')
    if cleaned['max_drawdown'] is not None and not 0 <= cleaned['max_drawdown'] <= 1:
        raise ValueError('Invalid replay drawdown')
    return {'provider': PROVIDER, 'completion': 'reported_complete' if payload.get('completion') == 'reported_complete' else 'unverified',
            'provider_status': 'local', 'progress': 1.0, 'backtest_id': None,
            'period': {'start': period['start'], 'end': period['end']}, 'metrics': cleaned}


def parse_any(payload):
    """One parser for the evidence archive: local replays and QC downloads."""
    if isinstance(payload, dict) and payload.get('provider') == PROVIDER:
        return parse_replay(payload)
    from backtests.quantconnect.results import parse_result
    return parse_result(payload)
