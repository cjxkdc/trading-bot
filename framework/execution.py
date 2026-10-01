"""Recoverable simulation execution; unknown/partial outcomes never cause resubmits."""
from dataclasses import asdict
from decimal import Decimal
from zoneinfo import ZoneInfo
from framework.decisions import digest
from framework.journal import packed, unpack_order
from framework.trading import amount, check_order, plan_orders


class ExecutionBlocked(RuntimeError):
    pass


class Executor:
    def __init__(self, broker, journal, policy, fees):
        if broker.mode not in {"paper", "broker_paper"}:
            raise ValueError("This release supports paper execution only")
        self.broker, self.journal, self.policy, self.fees = broker, journal, policy, fees

    def _reconcile(self, order, result):
        _, old = self.journal.status(order)
        filled = amount(result.filled_quantity)
        if (result.client_id != order.client_id
                or result.status not in {"pending", "accepted", "partial", "filled", "cancelled", "rejected", "unknown"}
                or filled > order.quantity or filled != filled.to_integral_value()
                or (old and filled < old.filled_quantity)
                or (old and old.broker_id and old.broker_id != result.broker_id)
                or (result.status == "filled" and filled != order.quantity)
                or (filled > 0 and not result.broker_id)
                or (filled > 0 and (amount(result.average_price, True) <= 0))):
            raise ExecutionBlocked("Inconsistent broker order state")
        if filled > 0 and ((order.side == "BUY" and result.average_price > order.limit_price)
                or (order.side == "SELL" and result.average_price < order.limit_price)):
            raise ExecutionBlocked("Broker fill violates the limit price")
        amount(result.fees)
        self.journal.save(result)
        if result.status != "filled":
            raise ExecutionBlocked(f"Order {result.status}: reconcile before continuing; no resubmit")

    def run(self, decision, quotes, now):
        with self.journal.exclusive():
            return self._run(decision, quotes, now)

    def _run(self, decision, quotes, now):
        if self.broker.mode == 'broker_paper':
            now = self.broker.execution_time()
        if (digest(decision.record) != decision.decision_id
                or decision.record["targets"] != [asdict(t) for t in decision.targets]):
            raise ExecutionBlocked("Decision was changed after signing")
        if now.tzinfo is None or now.astimezone(ZoneInfo("America/New_York")).date().isoformat() != decision.record["execution_day"]:
            raise ExecutionBlocked("Decision belongs to another execution session")
        account = self.broker.account()
        if self.broker.mode == 'broker_paper':
            now = self.broker.execution_time()
        cycle = account.account_id + ":" + decision.record["execution_day"]
        if self.journal.has_later_cycle(cycle):
            raise ExecutionBlocked("Cannot replay an older session into a newer account state")
        fingerprint = digest([decision.decision_id, packed(asdict(self.policy)), packed(asdict(self.fees))])
        frozen = self.journal.batch(cycle)
        if frozen and frozen[0] != fingerprint:
            raise ExecutionBlocked("Cycle already frozen with different decision/configuration")
        if self.journal.other_unfinished(cycle):
            raise ExecutionBlocked("Earlier cycle has unfinished orders")
        if frozen is None:
            if self.broker.open_orders():
                raise ExecutionBlocked("Broker has unresolved orders")
            context = decision.record.get("portfolio_context")
            if context is not None and set(context["held_asset_ids"]) != {
                    symbol for symbol, quantity in account.positions if quantity != 0}:
                raise ExecutionBlocked("Frozen portfolio context differs from the account holdings")
            orders = plan_orders(decision, account, quotes, now, self.policy, self.fees)
            self.journal.freeze(cycle, fingerprint, orders)
        orders = tuple(unpack_order(packed(o)) for o in self.journal.batch(cycle)[1])
        for order in orders:
            status, _ = self.journal.status(order)
            if status == "filled":
                continue
            if status != "planned":
                self._reconcile(order, self.broker.query(order.client_id))
                continue
            if self.broker.open_orders():
                raise ExecutionBlocked("Broker has unresolved orders")
            current_account = self.broker.account()
            if self.broker.mode == 'broker_paper':
                now = self.broker.execution_time()
                if now.astimezone(ZoneInfo('America/New_York')).date().isoformat() != decision.record['execution_day']:
                    raise ExecutionBlocked('Trading session changed; no further orders submitted')
            check_order(order, current_account, quotes, now, self.policy, self.fees)
            if not self.journal.claim(order):
                # Another worker owns the transmission. Do not race its query/save.
                raise ExecutionBlocked("Intent already claimed by another execution")
            result = self.broker.submit(order)  # Any exception leaves durable unknown.
            self._reconcile(order, result)
        return {"status": "complete", "decision_id": decision.decision_id, "orders": len(orders)}
