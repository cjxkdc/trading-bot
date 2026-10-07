"""Recoverable execution; live brokers require explicit activation."""
from dataclasses import asdict
from decimal import Decimal
import time
from zoneinfo import ZoneInfo
from framework.decisions import digest
from framework.contracts import SubmissionRejected
from framework.journal import packed, unpack_order
from framework.trading import amount, check_order, plan_orders


class ExecutionBlocked(RuntimeError):
    pass


class Executor:
    def __init__(self, broker, journal, policy, fees, *, settlement_seconds=0):
        if broker.mode not in {"paper", "broker_paper", "live"}:
            raise ValueError("Unsupported execution mode")
        if broker.mode == 'live' and getattr(broker, 'live_enabled', False) is not True:
            raise ValueError('Live execution must be explicitly enabled; paper remains the default')
        self.broker, self.journal, self.policy, self.fees = broker, journal, policy, fees
        if not 0 <= settlement_seconds <= 10:
            raise ValueError('Invalid settlement wait')
        self.settlement_seconds = settlement_seconds

    def _settle(self, order, state):
        deadline = time.monotonic() + self.settlement_seconds
        while True:
            can_wait = (state.status in {'accepted', 'pending', 'partial'} and bool(state.broker_id)
                        and time.monotonic() < deadline)
            self._reconcile(order, state, allow_pending=can_wait)
            if state.status == 'filled':
                return
            # Only query an acknowledged, validated order. Never transmit it again.
            time.sleep(.5)
            state = self.broker.query(order.client_id)

    def _reconcile(self, order, result, *, allow_pending=False):
        _, old = self.journal.status(order)
        filled = amount(result.filled_quantity)
        if (result.client_id != order.client_id
                or result.status not in {"pending", "accepted", "partial", "filled", "cancelled", "rejected", "unknown"}
                or filled > order.quantity or filled != filled.to_integral_value()
                or (old and filled < old.filled_quantity)
                or (old and old.broker_id and old.broker_id != result.broker_id)
                or (old and old.status in {'filled', 'cancelled', 'rejected'}
                    and (old.status != result.status or old.filled_quantity != filled))
                or (result.status == "filled" and filled != order.quantity)
                or (result.status == 'partial' and not 0 < filled < order.quantity)
                or (filled > 0 and not result.broker_id)
                or (filled > 0 and (amount(result.average_price, True) <= 0))):
            raise ExecutionBlocked("Inconsistent broker order state")
        if filled > 0 and ((order.side == "BUY" and result.average_price > order.limit_price)
                or (order.side == "SELL" and result.average_price < order.limit_price)):
            raise ExecutionBlocked("Broker fill violates the limit price")
        amount(result.fees)
        self.journal.save(result)
        if result.status != "filled" and not allow_pending:
            raise ExecutionBlocked(f"Order {result.status}: reconcile before continuing; no resubmit")

    def reconcile(self):
        """Refresh acknowledged/ambiguous intents without any transmission or planning."""
        if self.broker.mode != 'broker_paper':
            raise ExecutionBlocked('Automatic reconciliation requires a paper broker')
        with self.journal.exclusive():
            return self._reconcile_locked()

    def _reconcile_locked(self):
        account = self.broker.account()
        items = []
        queried = 0
        for cycle, order, status in self.journal.unfinished_orders():
            if cycle.rsplit(':', 1)[0] != account.account_id:
                raise ExecutionBlocked('Journal belongs to another account')
            if status not in {'planned', 'cancelled', 'rejected'}:
                self._reconcile(order, self.broker.query(order.client_id), allow_pending=True)
                status, _ = self.journal.status(order)
                queried += 1
            _, saved = self.journal.status(order)
            items.append({'client_id': order.client_id, 'status': status,
                          'symbol': order.symbol, 'side': order.side, 'quantity': str(order.quantity),
                          'filled_quantity': str(saved.filled_quantity) if saved else '0',
                          'execution_day': cycle.rsplit(':', 1)[1]})
        statuses = {item['status'] for item in items}
        state = ('blocked' if statuses & {'unknown', 'cancelled', 'rejected'} else
                 'waiting' if statuses & {'accepted', 'pending', 'partial'} else
                 'ready' if 'planned' in statuses else 'complete')
        batches = []
        for cycle, counts in self.journal.cycle_statuses().items():
            if cycle.rsplit(':', 1)[0] != account.account_id:
                raise ExecutionBlocked('Journal belongs to another account')
            batches.append({'execution_day': cycle.rsplit(':', 1)[1], 'statuses': counts,
                            'orders': sum(counts.values()), 'complete': not (set(counts)-{'filled'}),
                            'resolved': not (set(counts)-{'filled','abandoned'}),
                            'abandoned': counts.get('abandoned',0)})
        return {'recovery_state': state, 'orders': items, 'batches': batches,
                'abandoned': sum(b['abandoned'] for b in batches), 'queried': queried, 'submitted': 0}

    def close_unsubmitted(self):
        """Explicitly close unclaimed remainders; no broker cancellation or submission."""
        if self.broker.mode != 'broker_paper' or getattr(self.broker, 'read_only', False) is not True:
            raise ExecutionBlocked('Closing a remainder requires a read-only paper connection')
        with self.journal.exclusive():
            report = self._reconcile_locked()
            if report['recovery_state'] not in {'ready', 'complete'} or self.broker.open_orders():
                raise ExecutionBlocked('Unresolved broker orders prevent closing unsubmitted intents')
            count = self.journal.abandon_planned(self.broker.account().account_id)
            report = self._reconcile_locked()
            return dict(report, newly_abandoned=count)

    def run(self, decision, quotes, now, *, approved_orders=None, expected_account=None,
            recover=False, before_submit=None):
        if getattr(self.broker, 'read_only', False) is True:
            raise ExecutionBlocked('A read-only broker cannot start execution')
        with self.journal.exclusive():
            return self._run(decision, quotes, now, approved_orders=approved_orders,
                             expected_account=expected_account, recover=recover, before_submit=before_submit)

    def _recovered_account(self, baseline, orders):
        """Match inventory exactly and cash within the explicit per-fill fee budget.

        OpenD order replies omit commissions. Actual cash must remain inside the
        no-fee amount and configured conservative fee estimates; otherwise pause.
        """
        positions, cash, fee_budget = dict(baseline.positions), baseline.cash, Decimal(0)
        for order in orders:
            status, state = self.journal.status(order)
            if status == 'planned':
                continue
            if status != 'filled' or state is None:
                raise ExecutionBlocked(f'Order {status}: reconcile before continuing; no resubmit')
            sign = 1 if order.side == 'BUY' else -1
            positions[order.symbol] = positions.get(order.symbol, Decimal(0)) + sign * state.filled_quantity
            cash -= sign * state.filled_quantity * state.average_price
            fee_budget += max(state.fees, self.fees.estimate(state.filled_quantity, state.average_price))
        current = self.broker.account()
        expected_positions = tuple(sorted((s, q) for s, q in positions.items() if q))
        if ((current.account_id, current.currency, current.positions) !=
                (baseline.account_id, baseline.currency, expected_positions)
                or not cash-fee_budget-Decimal('.01') <= current.cash <= cash+Decimal('.01')):
            raise ExecutionBlocked('Account differs from recorded fills; automatic continuation paused')
        return current

    def _run(self, decision, quotes, now, *, approved_orders=None, expected_account=None,
             recover=False, before_submit=None):
        if self.broker.mode in {'broker_paper', 'live'}:
            now = self.broker.execution_time()
        if (digest(decision.record) != decision.decision_id
                or decision.record["targets"] != [asdict(t) for t in decision.targets]):
            raise ExecutionBlocked("Decision was changed after signing")
        if now.tzinfo is None or now.astimezone(ZoneInfo("America/New_York")).date().isoformat() != decision.record["execution_day"]:
            raise ExecutionBlocked("Decision belongs to another execution session")
        account = self.broker.account()
        if not recover and expected_account is not None and (account.account_id, account.currency, account.cash, account.positions) != (
                expected_account.account_id, expected_account.currency, expected_account.cash, expected_account.positions):
            raise ExecutionBlocked('Account changed after order preview; prepare again')
        if self.broker.mode in {'broker_paper', 'live'}:
            now = self.broker.execution_time()
        cycle = account.account_id + ":" + decision.record["execution_day"]
        if self.journal.has_later_cycle(cycle):
            raise ExecutionBlocked("Cannot replay an older session into a newer account state")
        fingerprint = digest([decision.decision_id, packed(asdict(self.policy)), packed(asdict(self.fees))])
        if approved_orders is not None:
            fingerprint = digest([fingerprint, packed([asdict(o) for o in approved_orders])])
        frozen = self.journal.batch(cycle)
        if frozen and frozen[0] != fingerprint:
            raise ExecutionBlocked("Cycle already frozen with different decision/configuration")
        if self.journal.other_unfinished(cycle):
            raise ExecutionBlocked("Earlier cycle has unfinished orders")
        if recover:
            if self.broker.mode != 'broker_paper' or expected_account is None or approved_orders is None or frozen is None:
                raise ExecutionBlocked('Recovery requires an existing approved paper batch and bound account')
            if account.account_id != expected_account.account_id or account.currency != expected_account.currency:
                raise ExecutionBlocked('Recovery account identity differs from the approved account')
            frozen_orders = tuple(unpack_order(packed(o)) for o in frozen[1])
            if frozen_orders != tuple(approved_orders):
                raise ExecutionBlocked('Recovery orders differ from the frozen batch')
            for order in frozen_orders:
                status, _ = self.journal.status(order)
                if status not in {'planned', 'filled', 'cancelled', 'rejected', 'abandoned'}:
                    self._reconcile(order, self.broker.query(order.client_id), allow_pending=True)
            account = self._recovered_account(expected_account, frozen_orders)
        if frozen is None:
            if self.broker.open_orders():
                raise ExecutionBlocked("Broker has unresolved orders")
            context = decision.record.get("portfolio_context")
            if context is not None and set(context["held_asset_ids"]) != {
                    symbol for symbol, quantity in account.positions if quantity != 0}:
                raise ExecutionBlocked("Frozen portfolio context differs from the account holdings")
            orders = (tuple(approved_orders) if approved_orders is not None
                      else plan_orders(decision, account, quotes, now, self.policy, self.fees))
            projected = account
            if len({o.client_id for o in orders}) != len(orders) or len(orders) > 200:
                raise ExecutionBlocked('Invalid prepared order batch')
            for order in orders:
                projected = check_order(order, projected, quotes, now, self.policy, self.fees)
            self.journal.freeze(cycle, fingerprint, orders)
        orders = tuple(unpack_order(packed(o)) for o in self.journal.batch(cycle)[1])
        for order in orders:
            status, _ = self.journal.status(order)
            if status == "filled":
                continue
            if status == 'abandoned':
                raise ExecutionBlocked('Unsubmitted intent was explicitly abandoned; never resume this batch')
            if status != "planned":
                self._settle(order, self.broker.query(order.client_id))
                continue
            if self.broker.open_orders():
                raise ExecutionBlocked("Broker has unresolved orders")
            current_account = (self._recovered_account(expected_account, orders) if recover
                               else self.broker.account())
            if self.broker.mode in {'broker_paper', 'live'}:
                now = self.broker.execution_time()
                if now.astimezone(ZoneInfo('America/New_York')).date().isoformat() != decision.record['execution_day']:
                    raise ExecutionBlocked('Trading session changed; no further orders submitted')
            check_order(order, current_account, quotes, now, self.policy, self.fees)
            if before_submit is not None:
                before_submit()
            claim = self.journal.claim(order)
            if claim is None:
                # Another worker owns the transmission. Do not race its query/save.
                raise ExecutionBlocked("Intent already claimed by another execution")
            try:
                result = self.broker.submit(order)
            except SubmissionRejected:
                # Only an explicit adapter proof from this new submission call
                # can classify a rejection. Every other exception stays unknown.
                self.journal.reject_unsubmitted(order, claim)
                raise
            finally:
                self.journal.release_claim(order, claim)
            self._settle(order, result)
        return {"status": "complete", "decision_id": decision.decision_id, "orders": len(orders)}
