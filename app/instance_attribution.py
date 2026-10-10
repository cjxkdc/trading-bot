"""Value and profit of one strategy instance, derived from the shared book, cycle fills and observations.

Ownership is an internal convention (framework/strategy_book.py): the broker
holds one net position, each instance owns a share of it. This module never
invents fills: a share change is priced at the cycle's average fill price when
the account traded that symbol in the same cycle, otherwise at the latest
observed price (a transfer between owners), and is reported as unpriced when
neither exists.
"""
from datetime import datetime, timezone
from decimal import Decimal

from framework.strategy_book import quantities

D = Decimal
LIMITATIONS = [
    '归属数量是内部约定，券商只看到合并持仓；估值按账户观测里的最新价计算，不含现金与利息。',
    '股数变化按同一轮账户成交的均价计价；没有对应成交的变化（持仓在实例间转移）按当时观测价计价。',
    '只覆盖保留的运行记录与账户观测；记录被清理后，更早的成本不再计入。',
]


def _stamp(value):
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _money(value):
    return None if value is None else float(round(D(str(value)), 2))


def _fill_prices(cycle, fills):
    """Average fill price per ticker for the account's net orders of this cycle."""
    prices = {}
    for order in cycle.get('orders') or []:
        fill = fills.get(order.get('client_order_id'))
        if not fill or fill.get('filled_quantity') in (None, '', '0') or fill.get('average_price') in (None, ''):
            continue
        try:
            filled, average = D(str(fill['filled_quantity'])), D(str(fill['average_price']))
        except ArithmeticError:
            continue
        if filled > 0 and average > 0:
            prices[order.get('ticker')] = average
    return prices


def _observed_price(observations, ticker, at):
    """Latest observed price for a ticker at or before ``at``."""
    for item in reversed(observations):
        if _stamp(item['as_of']) > at:
            continue
        for position in item.get('positions') or []:
            if position.get('symbol') == ticker:
                return _position_price(position)
    return None


def _position_price(position):
    price = position.get('last_price')
    if price is None and position.get('market_value') is not None and position.get('quantity'):
        try:
            return D(str(position['market_value'])) / D(str(position['quantity']))
        except (ArithmeticError, ValueError):
            return None
    return None if price is None else D(str(price))


def attribute(ident, cycles, observations, fills):
    """Build the valuation curve, trades and P&L of ``ident``.

    cycles: retained cycle logs of the instance's broker, any order; each has
    created_at, owner_positions (book after the cycle), orders (client_order_id, ticker).
    observations: daily account observations with priced positions, any order.
    fills: journal items keyed by client order id (filled_quantity, average_price).
    """
    cycles = sorted((c for c in cycles if c.get('created_at')), key=lambda c: _stamp(c['created_at']))
    observations = sorted((o for o in observations if o.get('as_of')), key=lambda o: _stamp(o['as_of']))
    fills = {item.get('id'): item for item in fills if isinstance(item, dict)}
    before, timeline, trades, invested = {}, [], [], D(0)
    unpriced = set()
    for cycle in cycles:
        owners = cycle.get('owner_positions') or {}
        if ident not in owners and ident not in (cycle.get('instance_ids') or []) and not before:
            continue  # a cycle on this book before the instance existed
        at = _stamp(cycle['created_at'])
        after = quantities(owners.get(ident, {}))
        prices = _fill_prices(cycle, fills)
        for ticker in sorted(set(after) | set(before)):
            delta = after.get(ticker, D(0)) - before.get(ticker, D(0))
            if not delta:
                continue
            basis, price = 'fill', prices.get(ticker)
            if price is None:
                basis, price = 'observation', _observed_price(observations, ticker, at)
            cost = None if price is None else delta * price
            if cost is None:
                unpriced.add(ticker)
                basis = None
            else:
                invested += cost
            trades.append({'cycle_id': cycle.get('cycle_id'), 'at': at.isoformat(), 'ticker': ticker,
                           'delta': str(delta), 'price': _money(price), 'cost': _money(cost), 'basis': basis,
                           'invested_after': _money(invested)})
        before = after
        timeline.append((at, after, invested))
    curve = []
    for item in observations:
        at = _stamp(item['as_of'])
        holdings, spent = {}, D(0)
        for moment, held, total in timeline:
            if moment <= at:
                holdings, spent = held, total
        value, missing = D(0), []
        for ticker, quantity in holdings.items():
            position = next((p for p in item.get('positions') or [] if p.get('symbol') == ticker), None)
            price = _position_price(position) if position else None
            if price is None:
                missing.append(ticker)
                continue
            value += quantity * price
        curve.append({'as_of': at.isoformat(), 'value': _money(value), 'invested': _money(spent),
                      'pnl': _money(value - spent), 'unpriced': missing})
    latest = curve[-1] if curve else None
    positions = []
    if latest and timeline:
        holdings = next((held for moment, held, _ in reversed(timeline) if moment <= _stamp(latest['as_of'])), {})
        last = observations[-1]
        for ticker, quantity in sorted(holdings.items()):
            position = next((p for p in last.get('positions') or [] if p.get('symbol') == ticker), None)
            price = _position_price(position) if position else None
            positions.append({'ticker': ticker, 'quantity': str(quantity), 'last_price': _money(price),
                              'value': _money(quantity * price) if price is not None else None})
    stale = bool(timeline and (not observations or timeline[-1][0] > _stamp(observations[-1]['as_of'])))
    valuation = None
    if latest:
        spent = D(str(latest['invested']))
        valuation = dict(latest, positions=positions, currency=observations[-1].get('currency'),
                         return_rate=float((D(str(latest['value'])) - spent) / spent) if spent > 0 else None,
                         stale=stale)
    if not timeline:
        message = '还没有完成的调仓记录，暂无可归因的持仓。'
    elif not observations:
        message = '账户还没有估值观测；开启账户监控后才能计算市值与盈亏。'
    elif stale:
        message = '最近一轮调仓晚于最新账户观测，市值与盈亏将在下次观测后更新。'
    else:
        message = ''
    return {'instance_id': ident, 'valuation': valuation, 'curve': curve, 'trades': list(reversed(trades)),
            'cycle_count': len(timeline), 'observation_count': len(curve),
            'unpriced': sorted(unpriced), 'message': message, 'limitations': LIMITATIONS}
