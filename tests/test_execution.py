import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock

from framework.contracts import OrderState
from framework.decisions import Decision, digest
from framework.execution import Executor, ExecutionBlocked
from framework.journal import Journal
from framework.portfolio import StockTarget
from framework.trading import Account, ExecutionPolicy, Fees, Quote, check_order, plan_orders
from platforms.paper import PaperBroker

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def decision(weights=(('A', .45), ('B', .45)), day="2026-09-01"):
    targets = tuple(StockTarget(s, w, 0.) for s, w in weights)
    record = {"execution_day": day, "targets": [asdict(t) for t in targets]}
    return Decision(digest(record), targets, record)


class PaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.quotes = {s: Quote(s, D(99), D(100), NOW) for s in ('A', 'B', 'C')}
        self.policy = ExecutionPolicy(max_gross=D('.9'), max_weight=D('.45'))
        self.fees = Fees()
        self.account_path = self.root / 'paper.sqlite'
        self.journal_path = self.root / 'journal.sqlite'
        PaperBroker.create(self.account_path, D(10000))
        self.broker = PaperBroker(self.account_path, self.quotes, NOW, self.fees)
        self.journal = Journal(self.journal_path)
        self.addCleanup(lambda: self.broker.close())
        self.addCleanup(lambda: self.journal.close())

    def executor(self, broker=None, policy=None):
        return Executor(broker or self.broker, self.journal, policy or self.policy, self.fees)

    def test_new_strategy_targets_become_cash_covered_whole_share_orders(self):
        result = self.executor().run(decision(), self.quotes, NOW)
        account = self.broker.account()
        self.assertEqual(result['orders'], 2)
        self.assertGreaterEqual(account.cash, 0)
        self.assertEqual(dict(account.positions), {'A': D(45), 'B': D(45)})
        self.assertEqual(account.cash, D(998))

    def test_restart_and_same_cycle_rerun_never_buys_twice(self):
        self.executor().run(decision(), self.quotes, NOW)
        before = self.broker.account()
        self.broker.close()
        self.journal.close()
        self.broker = PaperBroker(self.account_path, self.quotes, NOW, self.fees)
        self.journal = Journal(self.journal_path)
        self.executor().run(decision(), self.quotes, NOW)
        self.assertEqual(before, self.broker.account())
        self.assertEqual(self.broker.db.execute('SELECT count(*) FROM orders').fetchone()[0], 2)

    def test_lost_acknowledgement_reconciles_filled_order_without_resubmit(self):
        proxy = Mock(wraps=self.broker)
        proxy.mode = 'paper'
        original = self.broker.submit
        def lost(order):
            original(order)
            raise ConnectionError('ack lost')
        proxy.submit.side_effect = lost
        with self.assertRaises(ConnectionError):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        proxy.submit.side_effect = None
        self.executor(proxy).run(decision(), self.quotes, NOW)
        self.assertEqual(proxy.submit.call_count, 2)  # A once (lost ack), B once.
        self.assertEqual(dict(self.broker.account().positions), {'A': D(45), 'B': D(45)})

    def test_unknown_before_transmission_blocks_rerun_and_next_cycle(self):
        proxy = Mock(wraps=self.broker)
        proxy.mode = 'paper'
        proxy.submit.side_effect = ConnectionError('network unavailable')
        with self.assertRaises(ConnectionError):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        with self.assertRaisesRegex(ExecutionBlocked, 'unknown'):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        with self.assertRaisesRegex(ExecutionBlocked, 'Earlier cycle'):
            self.executor(proxy).run(decision(day='2026-09-02'), self.quotes, NOW + timedelta(days=1))
        self.assertEqual(proxy.submit.call_count, 1)

    def test_partial_rejected_cancelled_block_remaining_orders(self):
        proxy = Mock(wraps=self.broker)
        proxy.mode = 'paper'
        proxy.submit.side_effect = lambda o: OrderState(o.client_id, 'fake', 'partial', D(2), D(100), D(1))
        with self.assertRaisesRegex(ExecutionBlocked, 'partial'):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        self.assertEqual(proxy.submit.call_count, 1)
        row = self.journal.db.execute('SELECT id FROM intents WHERE status=\'partial\'').fetchone()
        for state in ('cancelled', 'rejected'):
            proxy.query.return_value = OrderState(row[0], 'fake', state, D(2), D(100), D(1))
            with self.assertRaises(ExecutionBlocked):
                self.executor(proxy).run(decision(), self.quotes, NOW)
        self.assertEqual(proxy.submit.call_count, 1)

    def test_wrong_order_id_and_decreasing_fills_are_rejected(self):
        proxy = Mock(wraps=self.broker)
        proxy.mode = 'paper'
        proxy.submit.side_effect = lambda o: OrderState('wrong-id', 'fake', 'filled', o.quantity, D(100))
        with self.assertRaisesRegex(ExecutionBlocked, 'Inconsistent'):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        self.assertEqual(self.journal.db.execute("SELECT status FROM intents ORDER BY rowid LIMIT 1").fetchone()[0], 'unknown')

    def test_config_or_decision_changes_cannot_rewrite_a_frozen_cycle(self):
        self.executor().run(decision(), self.quotes, NOW)
        with self.assertRaisesRegex(ExecutionBlocked, 'frozen'):
            self.executor().run(decision((('C', .45),)), self.quotes, NOW)
        with self.assertRaisesRegex(ExecutionBlocked, 'frozen'):
            self.executor(policy=replace(self.policy, max_order_value=D(90000))).run(decision(), self.quotes, NOW)

    def test_reconciliation_rejects_decreasing_fill_and_price_outside_limit(self):
        proxy = Mock(wraps=self.broker)
        proxy.mode = 'paper'
        proxy.submit.side_effect = lambda o: OrderState(o.client_id, 'fake', 'partial', D(2), D(100), D(1))
        with self.assertRaises(ExecutionBlocked):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        row = self.journal.db.execute("SELECT id FROM intents WHERE status='partial'").fetchone()
        proxy.query.return_value = OrderState(row[0], 'fake', 'partial', D(1), D(100), D(1))
        with self.assertRaisesRegex(ExecutionBlocked, 'Inconsistent'):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        proxy.query.return_value = OrderState(row[0], 'fake', 'filled', D(45), D(101), D(1))
        with self.assertRaisesRegex(ExecutionBlocked, 'limit price'):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        self.assertEqual(proxy.submit.call_count,1)

    def test_mutated_decision_and_wrong_session_block_before_submission(self):
        d = decision()
        d.record['execution_day'] = '2026-09-02'
        with self.assertRaisesRegex(ExecutionBlocked, 'changed'):
            self.executor().run(d, self.quotes, NOW)
        with self.assertRaisesRegex(ExecutionBlocked, 'another execution'):
            self.executor().run(decision(day='2026-09-02'), self.quotes, NOW)
        self.assertEqual(self.broker.account().positions, ())

    def test_existing_broker_orders_block_new_batch(self):
        proxy = Mock(wraps=self.broker)
        proxy.mode = 'paper'
        proxy.open_orders.return_value = (object(),)
        with self.assertRaisesRegex(ExecutionBlocked, 'unresolved'):
            self.executor(proxy).run(decision(), self.quotes, NOW)
        proxy.submit.assert_not_called()

    def test_live_execution_requires_explicit_activation(self):
        proxy = Mock(mode='live')
        with self.assertRaisesRegex(ValueError, 'paper'):
            self.executor(proxy)

    def test_cash_fees_limits_and_stale_quotes(self):
        bad = dict(self.quotes, A=replace(self.quotes['A'], as_of=NOW-timedelta(minutes=3)))
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.executor().run(decision(), bad, NOW)
        bad['A'] = replace(self.quotes['A'], as_of=NOW+timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, 'future'):
            self.executor().run(decision(), bad, NOW)
        with self.assertRaisesRegex(ValueError, 'cap'):
            plan_orders(decision(), self.broker.account(), self.quotes, NOW,
                        replace(self.policy, max_order_value=D(1)), self.fees)
        tiny = replace(self.broker.account(), cash=D(50))
        self.assertEqual(plan_orders(decision(), tiny, self.quotes, NOW, self.policy, self.fees), ())

    def test_reductions_are_confirmed_before_increases(self):
        self.executor().run(decision(), self.quotes, NOW)
        future = NOW + timedelta(days=1)
        self.broker.now = future
        quotes = {s: replace(q, as_of=future) for s, q in self.quotes.items()}
        self.broker.quotes = quotes
        self.executor().run(decision((('C', .45),), '2026-09-02'), quotes, future)
        orders = [json.loads(r[0]) for r in self.broker.db.execute('SELECT body FROM orders ORDER BY rowid')][2:]
        self.assertEqual([o['side'] for o in orders], ['SELL','SELL','BUY'])
        self.assertEqual(set(dict(self.broker.account().positions)), {'C'})
        with self.assertRaisesRegex(ExecutionBlocked, 'older session'):
            self.executor().run(decision(), self.quotes, NOW)

    def test_second_worker_cannot_enter_during_execution(self):
        second = Journal(self.journal_path)
        try:
            with self.journal.exclusive():
                with self.assertRaises(OSError):
                    with second.exclusive():
                        self.fail('second worker entered')
        finally:
            second.close()

    def test_missing_account_cannot_be_silently_recreated(self):
        with self.assertRaises(ValueError):
            PaperBroker(self.root/'missing.sqlite', self.quotes, NOW, self.fees)
        with self.assertRaises(FileExistsError):
            PaperBroker.create(self.account_path, 1)

    def test_empty_existing_journal_is_not_treated_as_a_new_account(self):
        empty = self.root/'empty.sqlite'
        empty.touch()
        with self.assertRaisesRegex(ValueError, 'lost state'):
            Journal(empty)


if __name__ == '__main__':
    unittest.main()
