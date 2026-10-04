"""USD, whole-share, long-only execution contracts and deterministic sizing."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from typing import Protocol
from framework.contracts import Broker, OrderIntent
from framework.decisions import digest

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

    def estimate(self, quantity, price):
        return self.per_order + quantity * price * self.bps / 10000


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


def validate_market(account, quotes, now, policy):
    fresh(account.as_of, now, policy.max_quote_age_seconds)
    if account.currency != "USD" or not account.account_id:
        raise ValueError("This runner requires a dedicated USD paper account")
    amount(account.cash)
    positions = dict(account.positions)
    if len(positions) != len(account.positions):
        raise ValueError("Duplicate account positions")
    for symbol, quantity in positions.items():
        if amount(quantity, True) != quantity.to_integral_value():
            raise ValueError("Only positive whole-share positions are supported")
        if symbol not in quotes:
            raise ValueError("Missing quote for held asset")
    for symbol, quote in quotes.items():
        if symbol != quote.symbol or amount(quote.bid, True) > amount(quote.ask, True):
            raise ValueError("Invalid quote")
        fresh(quote.as_of, now, policy.max_quote_age_seconds)
    return positions


def check_order(order, account, quotes, now, policy, fees):
    positions = validate_market(account, quotes, now, policy)
    if (order.side not in {"BUY", "SELL"} or not order.client_id
            or order.quantity != amount(order.quantity, True).to_integral_value()
            or order.symbol not in quotes):
        raise ValueError("Invalid whole-share order")
    quote = quotes[order.symbol]
    value = order.quantity * amount(order.limit_price, True)
    if value > policy.max_order_value:
        raise ValueError("Order exceeds notional limit")
    fee = fees.estimate(order.quantity, order.limit_price)
    held = positions.get(order.symbol, D(0))
    if order.side == "SELL":
        if order.quantity > held:
            raise ValueError("Cannot sell unowned shares")
        cash = account.cash + value - fee
        positions[order.symbol] = held - order.quantity
    else:
        if order.limit_price < quote.ask:
            raise ValueError("Local replay requires a marketable limit")
        cash = account.cash - value - fee
        positions[order.symbol] = held + order.quantity
    if cash < 0:
        raise ValueError("Insufficient cash including fees")
    if order.side == "SELL" and order.limit_price > quote.bid:
        raise ValueError("Local replay requires a marketable limit")
    gross = sum((qty * quotes[s].mark for s, qty in positions.items()), D(0))
    equity = cash + gross
    if order.side == "BUY" and (gross > policy.max_gross * equity
            or positions[order.symbol] * quote.mark > policy.max_weight * equity):
        raise ValueError("Post-fee exposure exceeds limit")
    return Account(account.account_id, account.currency, cash,
                   tuple(sorted((s, q) for s, q in positions.items() if q)), now)


def plan_orders(decision, account, quotes, now, policy, fees, *, split_orders=False, tick_size=None):
    positions = validate_market(account, quotes, now, policy)
    targets = {t.asset_id: amount(t.weight) for t in decision.targets}
    if (len(targets) != len(decision.targets) or any(w > policy.max_weight for w in targets.values())
            or sum(targets.values(), D(0)) > policy.max_gross):
        raise ValueError("Targets exceed execution policy")
    if any(s not in quotes for s in targets):
        raise ValueError("Missing target quote")
    equity = account.cash + sum((q * quotes[s].mark for s, q in positions.items()), D(0))
    desired = {s: (equity * w / quotes[s].ask).to_integral_value(rounding=ROUND_FLOOR)
               for s, w in targets.items()}
    orders, projected = [], account
    # Reductions precede increases. Each order is checked again at transmission.
    for side in ("SELL", "BUY"):
        for symbol in sorted(set(positions) | set(targets)):
            delta = desired.get(symbol, D(0)) - positions.get(symbol, D(0))
            if (side == "SELL" and delta >= 0) or (side == "BUY" and delta <= 0):
                continue
            price = quotes[symbol].bid if side == "SELL" else quotes[symbol].ask
            if tick_size is not None:
                tick = amount(tick_size,True)
                price = (price / tick).to_integral_value(rounding=ROUND_CEILING if side == 'BUY' else ROUND_FLOOR) * tick
                amount(price,True)
            client_id = digest([account.account_id, decision.decision_id, symbol, side])
            quantity = abs(delta)
            if quantity * price > policy.max_order_value and not split_orders:
                raise ValueError("Target change exceeds order cap; splitting is not implemented")
            if split_orders:
                cap = int(policy.max_order_value / price)
                if cap < 1:
                    raise ValueError('One share exceeds the order cap')
                remaining, part = quantity, 0
                while remaining:
                    quantity = min(remaining, D(cap))
                    part += 1
                    part_id = digest([client_id, 'part', part])
                    if side == 'BUY':
                        lo, hi = 0, int(quantity)
                        while lo < hi:
                            mid = (lo + hi + 1) // 2
                            try:
                                check_order(OrderIntent(part_id, symbol, side, D(mid), price),
                                            projected, quotes, now, policy, fees)
                            except ValueError:
                                hi = mid - 1
                            else:
                                lo = mid
                        quantity = D(lo)
                    if not quantity:
                        break
                    order = OrderIntent(part_id, symbol, side, quantity, price)
                    projected = check_order(order, projected, quotes, now, policy, fees)
                    orders.append(order)
                    remaining -= quantity
                    if len(orders) > 200:
                        raise ValueError('Too many orders for one cycle')
                continue
            if side == "BUY":
                # Find the largest permitted integer buy including post-fee limits.
                lo, hi = 0, int(quantity)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    trial = OrderIntent(client_id, symbol, side, D(mid), price)
                    try:
                        check_order(trial, projected, quotes, now, policy, fees)
                    except ValueError:
                        hi = mid - 1
                    else:
                        lo = mid
                quantity = D(lo)
            if not quantity:
                continue
            order = OrderIntent(client_id, symbol, side, quantity, price)
            projected = check_order(order, projected, quotes, now, policy, fees)
            orders.append(order)
    return tuple(orders)
