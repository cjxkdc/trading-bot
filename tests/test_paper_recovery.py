"""Crash/partial-fill recovery with durable ledgers; no network or real accounts."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.control_contracts import ControlError
from app.paper_recovery import reconcile_orders
from brokers.opend_observation import trading_sessions
from framework.contracts import OrderState, SubmissionRejected
from framework.execution import Executor, ExecutionBlocked
from framework.journal import Journal, packed
from framework.journal_reader import JournalReader
from framework.trading import ExecutionPolicy, Fees, Quote, plan_orders
from platforms.paper import PaperBroker
from test_execution import NOW, decision
from test_opend import BrokerCases


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.quotes = {s: Quote(s, D(99), D(100), NOW) for s in ('A', 'B')}
        self.policy = ExecutionPolicy(max_gross=D('.9'), max_weight=D('.45'))
        self.fees = Fees()
        self.path, self.jpath = self.root/'paper.sqlite', self.root/'journal.sqlite'
        PaperBroker.create(self.path, D(10000))
        self.broker = PaperBroker(self.path, self.quotes, NOW, self.fees)
        self.journal = Journal(self.jpath)
        self.addCleanup(lambda: self.broker.close()); self.addCleanup(lambda: self.journal.close())
        self.baseline = self.broker.account()
        self.orders = plan_orders(decision(), self.baseline, self.quotes, NOW, self.policy, self.fees)
        self.proxy = Mock(wraps=self.broker)
        self.proxy.mode = 'broker_paper'
        self.proxy.execution_time = Mock(return_value=NOW)

    def run_batch(self, **kwargs):
        return Executor(self.proxy, self.journal, self.policy, self.fees).run(
            decision(), self.quotes, NOW, approved_orders=self.orders,
            expected_account=self.baseline, **kwargs)

    def pending(self, status='partial'):
        self.proxy.submit.side_effect = lambda o: OrderState(
            o.client_id, 'order-1', status, D(2) if status == 'partial' else D(0),
            D(100) if status == 'partial' else D(0))
        with self.assertRaises(ExecutionBlocked): self.run_batch()
        state = self.journal.status(self.orders[0])[1]
        self.proxy.query.return_value = state

    def test_partial_waiting_is_polled_without_resubmitting_or_releasing_buys(self):
        self.pending()
        for _ in range(3):
            report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
            self.assertEqual(report['recovery_state'], 'waiting')
            self.assertEqual(report['submitted'], 0)
            with self.assertRaisesRegex(ExecutionBlocked, 'partial'):
                self.run_batch(recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)
        self.assertEqual(self.journal.status(self.orders[1])[0], 'planned')

    def test_unknown_cancelled_rejected_latch_blocked_never_resubmit(self):
        self.pending('unknown')
        for status in ('unknown', 'cancelled', 'rejected'):
            # Separate fixture state avoids pretending a terminal state can change.
            self.journal.db.execute("UPDATE intents SET state=NULL,status='unknown' WHERE id=?", (self.orders[0].client_id,))
            self.journal.db.commit()
            self.proxy.query.return_value = OrderState(self.orders[0].client_id, 'order-1', status, D(0))
            report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
            self.assertEqual(report['recovery_state'], 'blocked')
            with self.assertRaises(ExecutionBlocked): self.run_batch(recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_crash_after_fill_then_restart_continues_exact_original_remainder(self):
        def lost_ack(order):
            self.broker.submit(order)
            raise ConnectionError('lost acknowledgment')
        self.proxy.submit.side_effect = lost_ack
        with self.assertRaises(ConnectionError): self.run_batch()
        self.broker.close(); self.journal.close()
        self.broker = PaperBroker(self.path, self.quotes, NOW, self.fees)
        self.journal = Journal(self.jpath)
        self.proxy = Mock(wraps=self.broker); self.proxy.mode = 'broker_paper'
        self.proxy.execution_time = Mock(return_value=NOW)
        self.assertEqual(self.run_batch(recover=True)['status'], 'complete')
        self.proxy.submit.assert_called_once_with(self.orders[1])
        self.assertEqual(dict(self.broker.account().positions), {'A': D(45), 'B': D(45)})
        self.run_batch(recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_external_account_change_prevents_continuation(self):
        def lost_ack(order):
            self.broker.submit(order)
            raise ConnectionError()
        self.proxy.submit.side_effect = lost_ack
        with self.assertRaises(ConnectionError): self.run_batch()
        current = self.broker.account()
        for altered in (replace(current, cash=current.cash+D(100)),
                        replace(current, positions=(('A', D(44)),)),
                        replace(current, account_id='foreign-account')):
            self.proxy.account.return_value = altered
            with self.assertRaises(ExecutionBlocked): self.run_batch(recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_config_change_or_no_frozen_batch_never_enters_recovery(self):
        with self.assertRaisesRegex(ExecutionBlocked, 'existing approved'):
            self.run_batch(recover=True)
        self.pending('accepted')
        with self.assertRaisesRegex(ExecutionBlocked, 'configuration'):
            Executor(self.proxy, self.journal, replace(self.policy,max_order_value=D(99999)), self.fees).run(
                decision(), self.quotes, NOW, approved_orders=self.orders,
                expected_account=self.baseline, recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_new_session_allows_readonly_reconciliation_but_no_remaining_orders(self):
        self.pending('accepted')
        self.proxy.execution_time.return_value = NOW+timedelta(days=1)
        report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
        self.assertEqual(report['recovery_state'], 'waiting')
        with self.assertRaisesRegex(ExecutionBlocked, 'another execution session'):
            self.run_batch(recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_disable_guard_before_claim_keeps_remaining_intent_unsubmitted(self):
        def guard():
            if self.proxy.submit.call_count:
                raise ControlError('Automation paused')
        with self.assertRaisesRegex(ControlError, 'paused'):
            self.run_batch(before_submit=guard)
        self.assertEqual(self.proxy.submit.call_count, 1)
        self.assertEqual(self.journal.status(self.orders[1])[0], 'planned')

    def test_proven_presend_rejection_is_audited_and_never_retried(self):
        self.proxy.submit.side_effect = SubmissionRejected('Do not persist this private text')
        with self.assertRaises(SubmissionRejected):
            self.run_batch()
        state = self.journal.status(self.orders[0])[1]
        self.assertEqual((state.status, state.broker_id, state.filled_quantity), ('rejected', None, D(0)))
        self.assertEqual(self.journal.status(self.orders[1])[0], 'planned')
        audit = self.journal.db.execute('SELECT action,body FROM execution_audit').fetchall()
        self.assertEqual(audit[0][0], 'reject_before_transmission')
        self.assertNotIn('private text', str(audit))
        self.journal.close()
        self.journal = Journal(self.jpath)
        report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
        self.assertEqual(report['recovery_state'], 'blocked')
        self.proxy.query.assert_not_called()
        with self.assertRaises(ExecutionBlocked):
            self.run_batch(recover=True)
        self.proxy.submit.assert_called_once()

    def test_unknown_legacy_intent_is_never_reclassified_by_presend_rejection(self):
        self.pending('unknown')
        self.proxy.submit.side_effect = SubmissionRejected('not reached')
        with self.assertRaises(ExecutionBlocked):
            self.run_batch(recover=True)
        self.assertEqual(self.journal.status(self.orders[0])[0], 'unknown')
        self.proxy.submit.assert_called_once()
        self.assertIsNone(self.journal.db.execute("SELECT name FROM sqlite_master WHERE name='execution_audit'").fetchone())

    def test_restart_cannot_forge_fresh_claim_proof_for_unknown_without_state(self):
        self.proxy.submit.side_effect = ConnectionError('lost before acknowledgement')
        with self.assertRaises(ConnectionError):
            self.run_batch()
        self.assertEqual(self.journal.status(self.orders[0]), ('unknown', None))
        self.journal.close()
        self.journal = Journal(self.jpath)
        with self.journal.exclusive(), self.assertRaisesRegex(ValueError, 'current submission claim'):
            self.journal.reject_unsubmitted(self.orders[0], object())
        self.assertEqual(self.journal.status(self.orders[0]), ('unknown', None))

    def test_reconcile_application_uses_only_readonly_session(self):
        self.pending('accepted')
        @contextmanager
        def session(fees): yield self.proxy, self.journal
        control = SimpleNamespace(mode='broker_paper', state_dir=self.root, reconciliation_session=session)
        report = reconcile_orders(control=control, policy=self.policy, fees=self.fees)
        self.assertEqual(report['status'], 'paused')
        self.assertEqual(report['recovery_state'], 'waiting')
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_filled_position_not_yet_visible_waits_before_next_order(self):
        self.pending('accepted')
        self.proxy.query.return_value = OrderState(self.orders[0].client_id, 'order-1', 'filled',
                                                  self.orders[0].quantity, D(100))
        with self.assertRaisesRegex(ExecutionBlocked, 'recorded fills'):
            self.run_batch(recover=True)
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_completed_broker_order_cannot_hide_unsubmitted_frozen_remainder(self):
        self.pending('accepted')
        self.proxy.query.return_value = OrderState(self.orders[0].client_id, 'order-1', 'filled',
                                                  self.orders[0].quantity, D(100))
        report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
        self.assertEqual(report['recovery_state'], 'ready')
        self.assertEqual(report['batches'][0]['statuses'], {'filled': 1, 'planned': 1})
        self.assertFalse(report['batches'][0]['complete'])
        self.journal.db.execute('DELETE FROM intents WHERE id=?', (self.orders[1].client_id,))
        self.journal.db.commit()
        with self.assertRaisesRegex(ValueError, 'disagree'):
            Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()

    def freeze_without_transmission(self):
        def paused():raise ControlError('User stopped before submission')
        with self.assertRaises(ControlError):self.run_batch(before_submit=paused)
        self.proxy.read_only=True

    def test_explicit_abandon_keeps_original_intents_audit_and_never_replays_them(self):
        self.freeze_without_transmission()
        executor=Executor(self.proxy,self.journal,self.policy,self.fees)
        report=executor.close_unsubmitted()
        self.assertEqual(report['newly_abandoned'],2)
        self.assertEqual(report['recovery_state'],'complete')
        self.assertFalse(report['batches'][0]['complete'])
        self.assertTrue(report['batches'][0]['resolved'])
        self.assertEqual(report['batches'][0]['abandoned'],2)
        self.assertEqual(self.journal.unfinished_orders(),())
        self.assertFalse(JournalReader(self.jpath).has_unresolved())
        audit=self.journal.db.execute('SELECT action,body FROM execution_audit').fetchall()
        self.assertEqual(len(audit),1)
        self.assertEqual(audit[0][0],'abandon_unsubmitted')
        import json
        self.assertEqual([i['intent'] for i in json.loads(audit[0][1])['intents']],
                         json.loads(packed([asdict(o) for o in self.orders])))
        self.assertEqual(executor.close_unsubmitted()['newly_abandoned'],0)
        self.assertEqual(self.journal.db.execute('SELECT count(*) FROM execution_audit').fetchone()[0],1)
        self.proxy.submit.assert_not_called()
        with self.assertRaisesRegex(ExecutionBlocked,'read-only'):
            self.run_batch()
        self.proxy.read_only=False
        for recovering in (False,True):
            with self.assertRaisesRegex(ExecutionBlocked,'abandoned'):
                self.run_batch(recover=recovering)
        self.assertEqual([self.journal.status(o)[0] for o in self.orders],['abandoned','abandoned'])
        # A new session can plan against actual holdings; the old intents stay retired.
        future=NOW+timedelta(days=1)
        self.broker.now=future
        self.broker.quotes={s:replace(q,as_of=future) for s,q in self.quotes.items()}
        self.proxy.execution_time.return_value=future
        self.proxy.read_only=False  # A later execution opens a new order-capable paper session.
        result=executor.run(decision(day='2026-09-02'),self.broker.quotes,future)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(self.proxy.submit.call_count,2)

    def test_abandon_requires_lock_and_readonly_broker_and_no_open_orders(self):
        self.freeze_without_transmission()
        with self.assertRaisesRegex(RuntimeError,'exclusive'):
            self.journal.abandon_planned(self.baseline.account_id)
        self.proxy.read_only=False
        with self.assertRaisesRegex(ExecutionBlocked,'read-only'):
            Executor(self.proxy,self.journal,self.policy,self.fees).close_unsubmitted()
        self.proxy.read_only=True
        self.proxy.open_orders.return_value=('foreign-order',)
        with self.assertRaisesRegex(ExecutionBlocked,'Unresolved'):
            Executor(self.proxy,self.journal,self.policy,self.fees).close_unsubmitted()
        self.assertEqual([self.journal.status(o)[0] for o in self.orders],['planned','planned'])
        self.proxy.submit.assert_not_called()

    def test_abandon_cannot_erase_unknown_partial_or_terminal_failure(self):
        self.pending('accepted')
        self.proxy.read_only=True
        for status in ('unknown','accepted','pending','partial','rejected','cancelled'):
            quantity=D(2) if status=='partial' else D(0)
            state=OrderState(self.orders[0].client_id,'order-1',status,quantity,D(100) if quantity else D(0))
            self.journal.db.execute('UPDATE intents SET status=?,state=? WHERE id=?',
                                    (status,packed(asdict(state)),state.client_id))
            self.journal.db.commit()
            self.proxy.query.return_value=state
            with self.subTest(status=status),self.assertRaises(ExecutionBlocked):
                Executor(self.proxy,self.journal,self.policy,self.fees).close_unsubmitted()
            self.assertEqual(self.journal.status(self.orders[1])[0],'planned')
        self.assertEqual(self.proxy.submit.call_count,1)

    def test_explicit_closure_can_resolve_filled_plus_planned_without_rewriting_fills(self):
        def paused():
            if self.proxy.submit.call_count:raise ControlError('Stopped')
        with self.assertRaises(ControlError):self.run_batch(before_submit=paused)
        filled=self.journal.status(self.orders[0])
        self.proxy.read_only=True
        report=Executor(self.proxy,self.journal,self.policy,self.fees).close_unsubmitted()
        self.assertEqual(report['newly_abandoned'],1)
        self.assertEqual(self.journal.status(self.orders[0]),filled)
        self.assertEqual(self.journal.status(self.orders[1])[0],'abandoned')
        self.assertEqual(self.proxy.submit.call_count,1)

    def terminal(self, status='cancelled', filled=D(0)):
        self.proxy.submit.side_effect = lambda o: OrderState(o.client_id, 'order-1', status, filled,
                                                             D(100) if filled else D(0))
        with self.assertRaises(ExecutionBlocked): self.run_batch()
        state = self.journal.status(self.orders[0])[1]
        self.proxy.query.return_value = state
        self.proxy.read_only = True
        return state

    def test_confirmed_terminal_orders_close_with_audit_and_unblock_the_next_session(self):
        import json
        state = self.terminal('cancelled', D(2))
        self.assertEqual(Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()['terminal'], 1)
        report = Executor(self.proxy, self.journal, self.policy, self.fees).close_terminal()
        self.assertEqual((report['newly_closed'], report['newly_abandoned']), (1, 1))
        self.assertEqual(report['recovery_state'], 'complete')
        self.assertTrue(report['batches'][0]['resolved'])
        self.assertEqual(report['batches'][0]['closed'], 1)
        # The broker's own terminal state and partial fill are kept verbatim.
        self.assertEqual(self.journal.status(self.orders[0]), ('closed', state))
        self.assertEqual(self.journal.status(self.orders[1])[0], 'abandoned')
        self.assertFalse(JournalReader(self.jpath).has_unresolved())
        audit = self.journal.db.execute('SELECT action,body FROM execution_audit').fetchall()
        self.assertEqual([row[0] for row in audit], ['close_terminal'])
        self.assertEqual([i['status'] for i in json.loads(audit[0][1])['intents']], ['cancelled', 'planned'])
        self.proxy.read_only = False
        self.proxy.submit.side_effect = None  # Later sessions reach the paper broker again.
        for recovering in (False, True):
            with self.assertRaisesRegex(ExecutionBlocked, 'closed'):
                self.run_batch(recover=recovering)
        future = NOW + timedelta(days=1)
        self.broker.now = future
        self.broker.quotes = {s: replace(q, as_of=future) for s, q in self.quotes.items()}
        self.proxy.execution_time.return_value = future
        self.assertEqual(Executor(self.proxy, self.journal, self.policy, self.fees).run(
            decision(day='2026-09-02'), self.broker.quotes, future)['status'], 'complete')
        self.assertEqual(self.proxy.submit.call_count, 3)

    def test_close_terminal_refuses_ambiguous_changed_or_unconfirmed_states(self):
        self.terminal('rejected')
        executor = lambda: Executor(self.proxy, self.journal, self.policy, self.fees)
        self.proxy.read_only = False
        with self.assertRaisesRegex(ExecutionBlocked, 'read-only'):
            executor().close_terminal()
        self.proxy.read_only = True
        self.proxy.open_orders.return_value = ('foreign-order',)
        with self.assertRaisesRegex(ExecutionBlocked, 'reconciled'):
            executor().close_terminal()
        self.proxy.open_orders.return_value = ()
        saved = self.journal.status(self.orders[0])[1]
        self.proxy.query.return_value = replace(saved, broker_id='order-2')
        with self.assertRaisesRegex(ExecutionBlocked, 'no longer confirms'):
            executor().close_terminal()
        for status in ('unknown', 'accepted', 'pending', 'partial'):
            quantity = D(2) if status == 'partial' else D(0)
            state = OrderState(self.orders[0].client_id, 'order-1', status, quantity, D(100) if quantity else D(0))
            self.journal.db.execute('UPDATE intents SET status=?,state=? WHERE id=?',
                                    (status, packed(asdict(state)), state.client_id))
            self.journal.db.commit()
            self.proxy.query.return_value = state
            with self.subTest(status=status), self.assertRaises(ExecutionBlocked):
                executor().close_terminal()
        self.assertEqual([self.journal.status(o)[0] for o in self.orders], ['partial', 'planned'])
        self.assertIsNone(self.journal.db.execute("SELECT name FROM sqlite_master WHERE name='execution_audit'").fetchone())
        self.assertEqual(self.proxy.submit.call_count, 1)

    def test_close_terminal_needs_a_terminal_order_and_skips_query_for_presend_rejection(self):
        self.freeze_without_transmission()
        with self.assertRaisesRegex(ExecutionBlocked, 'No cancelled'):
            Executor(self.proxy, self.journal, self.policy, self.fees).close_terminal()
        self.proxy.read_only = False
        self.proxy.submit.side_effect = SubmissionRejected('pre-send')
        self.proxy.reset_mock()
        with self.assertRaises(SubmissionRejected): self.run_batch()
        self.proxy.read_only = True
        report = Executor(self.proxy, self.journal, self.policy, self.fees).close_terminal()
        self.assertEqual(report['newly_closed'], 1)
        self.proxy.query.assert_not_called()

    def test_blocked_message_offers_terminal_closure_only_when_nothing_is_ambiguous(self):
        @contextmanager
        def session(fees): yield self.proxy, self.journal
        control = SimpleNamespace(mode='broker_paper', state_dir=self.root, reconciliation_session=session)
        self.terminal('cancelled')
        self.assertIn('关闭已撤单/拒单', reconcile_orders(control=control, policy=self.policy, fees=self.fees)['message'])
        self.journal.db.execute("UPDATE intents SET state=NULL,status='unknown' WHERE id=?", (self.orders[0].client_id,))
        self.journal.db.commit()
        self.proxy.query.return_value = OrderState(self.orders[0].client_id, 'order-1', 'unknown', D(0))
        self.assertNotIn('关闭已撤单/拒单', reconcile_orders(control=control, policy=self.policy, fees=self.fees)['message'])


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.connector = SimpleNamespace(mode='broker_paper',environment='SIMULATE',_settings=lambda:None)
        self.sdk = SimpleNamespace(TradeDateMarket=SimpleNamespace(US='US-OFFICIAL'))
        @contextmanager
        def connection(_): yield self.sdk, object(), {}
        p = patch('brokers.opend_observation.connection',connection); p.start(); self.addCleanup(p.stop)

    def test_holiday_and_early_close_are_preserved_from_official_calendar(self):
        rows = [{'time':'2026-11-25','trade_date_type':'WHOLE'},
                {'time':'2026-11-27','trade_date_type':'MORNING'}]
        with patch('brokers.opend_observation.call',return_value=rows) as call:
            days = trading_sessions(self.connector,date(2026,11,25),date(2026,11,27))
        self.assertEqual(days,[date(2026,11,25),date(2026,11,27)])
        self.assertEqual(call.call_args.kwargs['market'],'US-OFFICIAL')

    def test_empty_calendar_is_not_guessed_and_invalid_dates_rejected(self):
        with patch('brokers.opend_observation.call',return_value=[]):
            self.assertEqual(trading_sessions(self.connector,date(2026,11,26),date(2026,11,26)),[])
        for rows in ([{'time':'2026-11-24'}],[{'time':'2026-11-25'},{'time':'2026-11-25'}],None):
            with patch('brokers.opend_observation.call',return_value=rows), self.assertRaises(ControlError):
                trading_sessions(self.connector,date(2026,11,25),date(2026,11,27))

    def test_live_or_unbounded_calendar_request_is_blocked(self):
        with self.assertRaises(ControlError):
            trading_sessions(self.connector,date(2026,1,1),date(2028,1,1))
        self.connector.mode='live'
        with self.assertRaises(ControlError):
            trading_sessions(self.connector,date(2026,1,1),date(2026,1,2))


class ReadonlyOpenDTests(unittest.TestCase):
    provider = 'moomoo'
    setUp = BrokerCases.setUp
    make_broker = BrokerCases.make_broker
    place = BrokerCases.place
    cancel = BrokerCases.cancel

    def test_readonly_gateway_can_query_existing_intent_but_cannot_send_or_cancel(self):
        from platforms.opend import OpenDError
        accepted = self.broker.submit(self.intent)
        self.assertEqual(accepted.status, 'accepted')
        self.client._allow_orders = False
        readonly = self.make_broker(read_only=True)
        self.assertEqual(readonly.query(self.intent.client_id).status, 'accepted')
        with self.assertRaisesRegex(OpenDError, 'cannot transmit'):
            readonly.submit(self.intent)
        with self.assertRaisesRegex(OpenDError, 'cannot cancel'):
            readonly.cancel(accepted.broker_id)
        self.client.place_order.assert_called_once()
        self.client.modify_order.assert_not_called()

    def test_readonly_mode_rejects_connection_with_order_permission(self):
        from platforms.opend import OpenDError
        with self.assertRaises(OpenDError):
            self.make_broker(read_only=True)


class RecoveryWorkerTests(unittest.TestCase):
    def test_recovery_errors_latch_blocked_but_known_pending_keeps_waiting(self):
        from app.portal_worker import dispatch
        for error in (ControlError('Changed configuration'), ExecutionBlocked('Wrong account'), ValueError('Invalid data')):
            with patch('app.portal_worker.perform',side_effect=error):
                result=dispatch('recover')
            self.assertEqual(result['recovery_state'],'blocked')
        with patch('app.portal_worker.perform',return_value={
                'status':'paused','recovery_state':'waiting','message':'Order partially filled'}):
            result=dispatch('recover')
        self.assertEqual(result['recovery_state'],'waiting')
