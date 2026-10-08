"""Instrument-unit ownership and net orders for a shared long-only account.

No broker imports. Ownership is an internal bookkeeping convention; one broker
position may have several owners. Idle owners keep exact shares, not weights.
"""
from decimal import Decimal, ROUND_FLOOR

from framework.trading import amount
from framework.market import InstrumentRules

D = Decimal


def quantities(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid ownership map')
    result = {}
    for symbol, raw in value.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError('Invalid ownership symbol')
        number = amount(raw, True)
        result[symbol] = number
    return result


def aggregate(owners):
    total = {}
    for positions in owners.values():
        for symbol, q in quantities(positions).items():
            total[symbol] = total.get(symbol, D(0)) + q
    return total


def reconcile_ownership(owners, observed, unmanaged=None):
    """Preserve pre-existing external shares; unexplained owned changes block."""
    owned = aggregate(owners)
    actual = quantities(observed)
    prior = quantities(unmanaged or {})
    for symbol, q in owned.items():
        if actual.get(symbol, D(0)) < q + prior.get(symbol, D(0)):
            raise ValueError('Attributed holdings differ from the broker; reconcile first')
    # External additions are explicitly owned by the unmanaged bucket.
    return {s: str(q - owned.get(s, D(0))) for s, q in actual.items() if q > owned.get(s, D(0))}


def _rule(symbol, instrument_rules):
    return InstrumentRules.from_record(instrument_rules[symbol]) if instrument_rules is not None else InstrumentRules()


def owner_plan(owners, unmanaged, due_targets, budgets, equity, asks, *, instrument_rules=None):
    """Keep inactive owners intact; size only due owners within their NAV budgets."""
    result = {owner: {s: str(q) for s, q in quantities(pos).items()} for owner, pos in owners.items()}
    for owner, targets in due_targets.items():
        budget = amount(budgets[owner], True)
        if budget > 1:
            raise ValueError('Budget exceeds account equity')
        result[owner] = {}
        for symbol, weight in targets.items():
            w = amount(weight, True)
            rule = _rule(symbol, instrument_rules)
            q = rule.floor_quantity(amount(equity, True) * budget * w / (amount(asks[symbol], True) * rule.contract_multiplier))
            if q < rule.min_quantity:
                q = D(0)
            if q:
                result[owner][symbol] = str(q)
    net = aggregate(result)
    for symbol, q in quantities(unmanaged).items():
        net[symbol] = net.get(symbol, D(0)) + q
    return result, {s: str(q) for s, q in sorted(net.items()) if q}


def settle_ownership(planned, previous, unmanaged, observed, due_ids, *, partial=False, instrument_rules=None):
    """Allocate actual instrument quantities after a cash/fee constrained net execution.

    Fixed owners and external shares receive their exact holdings first. The
    remaining shares go to due owners by largest remainder, deterministic on ID.
    This does not invent fills or allocate an unconfirmed broker position.
    """
    actual = quantities(observed)
    fixed = {key: pos for key, pos in previous.items() if key not in due_ids}
    reserved = aggregate(fixed)
    for s, q in quantities(unmanaged).items():
        reserved[s] = reserved.get(s, D(0)) + q
    effective = {key: dict(pos) for key, pos in planned.items()}
    if partial:
        for key in due_ids:
            for s, q in quantities(previous.get(key, {})).items():
                if q > amount(effective.setdefault(key, {}).get(s, 0)):
                    effective[key][s] = str(q)
    expected = aggregate(effective)
    for s, q in quantities(unmanaged).items():
        expected[s] = expected.get(s, D(0)) + q
    if any(actual.get(s, D(0)) < q for s, q in reserved.items()):
        raise ValueError('Execution reduced another owner\'s shares')
    if any(q > expected.get(s, D(0)) for s, q in actual.items()):
        raise ValueError('Unexpected broker shares after execution')
    result = {key: dict(pos) for key, pos in fixed.items()}
    result.update({key: {} for key in due_ids})
    for symbol, held in actual.items():
        step = _rule(symbol, instrument_rules).quantity_step
        units = (held - reserved.get(symbol, D(0))) / step
        if units != units.to_integral_value():
            raise ValueError('Observed position does not match the declared instrument quantity step')
        available = int(units)
        wanted_quantities = {key: quantities(planned.get(key, {})).get(symbol, D(0)) / step for key in due_ids}
        if any(q != q.to_integral_value() for q in wanted_quantities.values()):
            raise ValueError('Planned ownership does not match the declared instrument quantity step')
        wanted = {key: int(q) for key, q in wanted_quantities.items()}
        if partial and not sum(wanted.values()):
            wanted = {key: int(quantities(previous.get(key, {})).get(symbol, D(0)) / step) for key in due_ids}
        denominator = sum(wanted.values())
        if available and not denominator:
            raise ValueError('Unattributed fill')
        if not denominator:
            continue
        exact = {key: D(available) * count / denominator for key, count in wanted.items()}
        assigned = {key: int(value) for key, value in exact.items()}
        remainder = available - sum(assigned.values())
        ranking = sorted(wanted, key=lambda key: (-(exact[key] - assigned[key]), key))
        for key in ranking[:remainder]:
            assigned[key] += 1
        for key, count in assigned.items():
            if count:
                result[key][symbol] = str(D(count) * step)
    return result
