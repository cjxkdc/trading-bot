"""Currency-aware, long-only cash execution with module-defined instrument increments."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from typing import Protocol
from framework.contracts import Broker, OrderIntent
from framework.decisions import digest
from framework.market import ExecutionContext, InstrumentRules, execution_context as normalize_context, rules_for

D = Decimal


def amount(value, positive=False):
    if isinstance(value, bool):
        raise ValueError("Boolean is not an amount")
    value = D(str(value))
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError("Invalid financial amount")
    return value


@dataclass(frozen=True)
class Quote:
    symbol: str  # Stable asset ID; ticker translation belongs to the adapter.
    bid: Decimal
    ask: Decimal
    as_of: datetime
    rules: InstrumentRules | None = None
    currency: str | None = None

    @classmethod
    def from_record(cls, value):
        stamp = value["as_of"]
        return cls(value.get("symbol", value.get("asset_id")), amount(value["bid"], True),
                   amount(value["ask"], True), datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp,
                   InstrumentRules.from_record(value["rules"]) if value.get("rules") is not None else None,
                   value.get("currency"))

    @property
    def mark(self):
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class Account:
    account_id: str
    currency: str
    cash: Decimal
    positions: tuple[tuple[str, Decimal], ...]
    as_of: datetime


class ExecutionBroker(Broker, Protocol):
    mode: str
    def account(self) -> Account: ...
    def open_orders(self) -> tuple: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Fees:
    per_order: Decimal = D("1")
    bps: Decimal = D("0")

    def __post_init__(self):
        amount(self.per_order)
        if amount(self.bps) >= 10000:
            raise ValueError("Invalid fee rate")

    def estimate(self, quantity, price, multiplier=D("1")):
        return self.per_order + quantity * price * multiplier * self.bps / 10000


@dataclass(frozen=True)
class ExecutionPolicy:
    max_gross: Decimal = D("0.95")
    max_weight: Decimal = D("0.19")
    max_order_value: Decimal = D("100000")
    max_quote_age_seconds: int = 120

    def __post_init__(self):
        if not D(0) < amount(self.max_weight) <= amount(self.max_gross) <= 1:
            raise ValueError("Invalid exposure limits")
        amount(self.max_order_value, True)
        if type(self.max_quote_age_seconds) is not int or self.max_quote_age_seconds <= 0:
            raise ValueError("Invalid freshness limit")


def fresh(stamp, now, max_age):
    if stamp.tzinfo is None or now.tzinfo is None:
        raise ValueError("Timezone-aware timestamps required")
    if not 0 <= (now - stamp).total_seconds() <= max_age:
        raise ValueError("Stale or future account/quote")


def validate_market(account, quotes, now, policy, *, execution_context=None):
    fresh(account.as_of, now, policy.max_quote_age_seconds)
    if not isinstance(account.currency, str) or not account.currency or not account.account_id:
        raise ValueError("Account must declare its settlement currency and identity")
    if execution_context is not None and account.currency != normalize_context(execution_context).currency:
        raise ValueError("Account currency does not match the execution context; explicit FX conversion is required")
    amount(account.cash)
    positions = dict(account.positions)
    if len(positions) != len(account.positions):
        raise ValueError("Duplicate account positions")
    for symbol, quantity in positions.items():
        if amount(quantity, True) % rules_for(symbol, quotes, execution_context).quantity_step:
            raise ValueError("Position quantity does not match the instrument increment")
        if symbol not in quotes:
            raise ValueError("Missing quote for held asset")
    for symbol, quote in quotes.items():
        if symbol != quote.symbol or amount(quote.bid, True) > amount(quote.ask, True):
            raise ValueError("Invalid quote")
        if quote.currency is not None and quote.currency != account.currency:
            raise ValueError("Quote currency differs from account currency; explicit FX conversion is required")
        fresh(quote.as_of, now, policy.max_quote_age_seconds)
    return positions


def check_order(order, account, quotes, now, policy, fees, *, execution_context=None):
    positions = validate_market(account, quotes, now, policy, execution_context=execution_context)
    rule = rules_for(order.symbol, quotes, execution_context)
    if (order.side not in {"BUY", "SELL"} or not order.client_id
            or not rule.valid_quantity(amount(order.quantity, True))
            or order.symbol not in quotes):
        raise ValueError("Invalid order quantity for the instrument")
    quote = quotes[order.symbol]
    if amount(order.limit_price, True) % rule.price_tick:
        raise ValueError("Limit price does not match the instrument tick")
    value = order.quantity * order.limit_price * rule.contract_multiplier
    if value > policy.max_order_value:
        raise ValueError("Order exceeds notional limit")
    fee = fees.estimate(order.quantity, order.limit_price, rule.contract_multiplier)
    held = positions.get(order.symbol, D(0))
    if order.side == "SELL":
        if order.quantity > held:
            raise ValueError("Cannot sell unowned instrument units")
        cash = account.cash + value - fee
        positions[order.symbol] = held - order.quantity
    else:
        if order.limit_price < quote.ask:
            raise ValueError("Execution requires a marketable limit")
        cash = account.cash - value - fee
        positions[order.symbol] = held + order.quantity
    if cash < 0:
        raise ValueError("Insufficient cash including fees")
    if order.side == "SELL" and order.limit_price > quote.bid:
        raise ValueError("Execution requires a marketable limit")
    gross = sum((qty * quotes[s].mark * rules_for(s, quotes, execution_context).contract_multiplier for s, qty in positions.items()), D(0))
    equity = cash + gross
    if order.side == "BUY" and (gross > policy.max_gross * equity
            or positions[order.symbol] * quote.mark * rule.contract_multiplier > policy.max_weight * equity):
        raise ValueError("Post-fee exposure exceeds limit")
    return Account(account.account_id, account.currency, cash,
                   tuple(sorted((s, q) for s, q in positions.items() if q)), now)


def plan_orders(decision, account, quotes, now, policy, fees, *, split_orders=False, tick_size=None, execution_context=None):
    if execution_context is None:
        execution_context = decision.record.get("execution_context")
    positions = validate_market(account, quotes, now, policy, execution_context=execution_context)
    targets = {t.asset_id: amount(t.weight) for t in decision.targets}
    if (len(targets) != len(decision.targets) or any(w > policy.max_weight for w in targets.values())
            or sum(targets.values(), D(0)) > policy.max_gross):
        raise ValueError("Targets exceed execution policy")
    if any(s not in quotes for s in targets):
        raise ValueError("Missing target quote")
    equity = account.cash + sum((q * quotes[s].mark * rules_for(s, quotes, execution_context).contract_multiplier for s, q in positions.items()), D(0))
    desired = {s: rules_for(s, quotes, execution_context).floor_quantity(
        equity * w / (quotes[s].ask * rules_for(s, quotes, execution_context).contract_multiplier)) for s, w in targets.items()}
    # Managed portfolios freeze instrument-unit ownership before the net account batch.
    # The decision is recomputed by its runner before submission and recovery.
    if 'desired_positions' in decision.record:
        frozen = decision.record['desired_positions']
        if not isinstance(frozen, dict) or set(frozen) != set(targets):
            raise ValueError('Invalid managed quantity targets')
        desired = {s: amount(q, True) for s, q in frozen.items()}
        if any(not rules_for(s, quotes, execution_context).valid_quantity(q)
               or q * quotes[s].ask * rules_for(s, quotes, execution_context).contract_multiplier > equity * targets[s] + D('0.000001')
               for s, q in desired.items()):
            raise ValueError('Managed quantities exceed frozen weight targets')
    orders, projected = [], account
    # Reductions precede increases. Each order is checked again at transmission.
    for side in ("SELL", "BUY"):
        for symbol in sorted(set(positions) | set(targets)):
            delta = desired.get(symbol, D(0)) - positions.get(symbol, D(0))
            if (side == "SELL" and delta >= 0) or (side == "BUY" and delta <= 0):
                continue
            rule = rules_for(symbol, quotes, execution_context)
            price = rule.round_price(quotes[symbol].bid if side == "SELL" else quotes[symbol].ask, side)
            if tick_size is not None:
                tick = amount(tick_size,True)
                price = (price / tick).to_integral_value(rounding=ROUND_CEILING if side == 'BUY' else ROUND_FLOOR) * tick
                amount(price,True)
            client_id = digest([account.account_id, decision.decision_id, symbol, side])
            quantity = abs(delta)
            if quantity * price * rule.contract_multiplier > policy.max_order_value and not split_orders:
                raise ValueError("Target change exceeds order cap; splitting is not implemented")
            if split_orders:
                cap = rule.floor_quantity(policy.max_order_value / (price * rule.contract_multiplier))
                if cap < rule.min_quantity:
                    raise ValueError('Minimum instrument quantity exceeds the order cap')
                remaining, part = quantity, 0
                while remaining:
                    quantity = min(remaining, D(cap))
                    part += 1
                    part_id = digest([client_id, 'part', part])
                    if side == 'BUY':
                        quantity = _largest_buy(part_id, symbol, quantity, price, projected,
                                                quotes, now, policy, fees, rule, execution_context)
                    if quantity < rule.min_quantity:
                        break
                    order = OrderIntent(part_id, symbol, side, quantity, price)
                    projected = check_order(order, projected, quotes, now, policy, fees, execution_context=execution_context)
                    orders.append(order)
                    remaining -= quantity
                    if len(orders) > 200:
                        raise ValueError('Too many orders for one cycle')
                continue
            if side == "BUY":
                quantity = _largest_buy(client_id, symbol, quantity, price, projected,
                                        quotes, now, policy, fees, rule, execution_context)
            if quantity < rule.min_quantity:
                continue
            order = OrderIntent(client_id, symbol, side, quantity, price)
            projected = check_order(order, projected, quotes, now, policy, fees, execution_context=execution_context)
            orders.append(order)
    return tuple(orders)


def _largest_buy(client_id, symbol, quantity, price, account, quotes, now, policy, fees, rule, context):
    """Search feasible units above the minimum; zero is a separate outcome."""
    if quantity < rule.min_quantity:
        return D(0)
    minimum = OrderIntent(client_id, symbol, 'BUY', rule.min_quantity, price)
    try:
        check_order(minimum, account, quotes, now, policy, fees, execution_context=context)
    except ValueError:
        return D(0)
    lo, hi = int(rule.min_quantity / rule.quantity_step), int(quantity / rule.quantity_step)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        trial = OrderIntent(client_id, symbol, 'BUY', D(mid) * rule.quantity_step, price)
        try:
            check_order(trial, account, quotes, now, policy, fees, execution_context=context)
        except ValueError:
            hi = mid - 1
        else:
            lo = mid
    return D(lo) * rule.quantity_step
