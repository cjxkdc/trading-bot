import importlib.util
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from assembly import _build_platform, inspect_broker, open_broker_paper_session
from bot import TIGER_PAPER_CONFIG, DEFAULT_CONFIG, run_paper
from framework.configuration import load_config, normalize_config, fees_from_config
from framework.contracts import OrderIntent
from framework.execution import Executor, ExecutionBlocked
from framework.journal import Journal
from framework.trading import ExecutionPolicy, Quote
from platforms.brokers.tiger_paper import (TigerPaperConnector, TigerPaperBroker, TigerPaperError,
    _connect_paper, _orders, _paper_client_class, _status)
from test_execution import decision

HAS_SDK = importlib.util.find_spec('tigeropen') is not None
ACCOUNT = '20261001000000001'
NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)


def fixture_client():
    client = Mock(spec=['get_managed_accounts', 'get_prime_assets', 'get_positions',
                       'get_orders', 'get_order', 'place_order', 'cancel_order'])
    client._paper_account, client._paper_verified = ACCOUNT, True
    client.get_managed_accounts.return_value = [NS(account=ACCOUNT, account_type='PAPER')]
    client.get_prime_assets.return_value = NS(account=ACCOUNT, segments={'S': NS(
        buying_power=9999999, cash_balance=999999,
        currency_assets={'USD': NS(cash_balance=10000, cash_available_for_trade=9000),
                         'HKD': NS(cash_balance=90000, cash_available_for_trade=90000)})})
    client.get_positions.return_value = []
    client.get_orders.return_value = NS(result=[], next_page_token=None)
    client.get_order.return_value = None
    return client


class PaperRegistrationTests(unittest.TestCase):
    def test_simulation_config_does_not_change_strategy_or_local_replay(self):
        cfg = normalize_config(load_config(TIGER_PAPER_CONFIG))
        local = normalize_config(load_config(DEFAULT_CONFIG))
        self.assertEqual(cfg['broker']['type'], 'tiger_paper')
        self.assertEqual(local['broker']['type'], 'paper')
        self.assertEqual(cfg['strategy'], local['strategy'])
        connector = _build_platform(cfg['broker'], 'broker')
        self.assertEqual(connector.mode, 'broker_paper')
        with tempfile.TemporaryDirectory() as folder, patch(
                'platforms.brokers.tiger_paper._connect_paper') as connect:
            result = inspect_broker('tiger_paper', folder)
            self.assertEqual(result['status'], 'not_ready')
            self.assertFalse(result['network_requested'])
            connect.assert_not_called()

    def test_local_command_cannot_accidentally_connect_to_tiger(self):
        from platforms.data.snapshot import demo_snapshot
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            path.write_text(json.dumps(demo_snapshot()))
            with patch('platforms.brokers.tiger_paper._connect_paper') as connect:
                with self.assertRaisesRegex(ValueError, 'paper connections only'):
                    run_paper(path, TIGER_PAPER_CONFIG, Path(folder) / 'state')
                connect.assert_not_called()

    def test_second_state_directory_rejected_before_network_or_initialization(self):
        cfg = normalize_config(load_config(TIGER_PAPER_CONFIG))
        with tempfile.TemporaryDirectory() as folder, patch(
                'platforms.brokers.tiger_paper._connect_paper') as connect:
            other = Path(folder) / 'other'
            with self.assertRaisesRegex(ValueError, 'state directory'):
                with open_broker_paper_session(cfg, None, other, fees_from_config(cfg)):
                    self.fail('opened second ledger')
            self.assertFalse(other.exists())
            connect.assert_not_called()


@unittest.skipUnless(HAS_SDK, 'Install requirements-tiger.txt for Tiger paper contracts')
class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'props'
        self.config.mkdir()
        (self.config / 'tiger_openapi_config.properties').write_text(
            f'tiger_id=TEST\naccount={ACCOUNT}\nlicense=TBAU\nprivate_key_pk8=TEST-ONLY\nenv=PROD\n')
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.connector = TigerPaperConnector(str(self.config), str(self.state))
        self.connector.initialize(self.state, '99999999')
        self.client = fixture_client()
        self.path = self.state / 'tiger-paper.sqlite'
        self.broker = TigerPaperBroker(self.client, ACCOUNT, self.path, clock=lambda: NOW)
        self.addCleanup(lambda: self.broker.close())
        self.intent = OrderIntent('test-client-1', 'AAPL', 'BUY', D(10), D(100))
        self.remotes = {}

        def place(*, order):
            oid = len(self.remotes) + 1
            remote = NS(account=order.account, id=oid, contract=order.contract, action=order.action,
                        order_type=order.order_type, quantity=order.quantity, limit_price=order.limit_price,
                        quantity_scale=0, filled_scale=0, filled=0, avg_fill_price=0, commission=0,
                        user_mark=order.user_mark, status='Submitted')
            self.remotes[oid] = remote
            return oid

        self.client.place_order.side_effect = place
        self.client.get_order.side_effect = lambda **kw: self.remotes.get(kw['id'])

    def test_real_account_rejected_before_client_or_network_construction(self):
        path = self.config / 'tiger_openapi_config.properties'
        path.write_text(path.read_text().replace(ACCOUNT, '12345678'))
        with patch('platforms.brokers.tiger_paper._paper_client_class') as cls:
            self.assertEqual(self.connector.inspect()['status'], 'not_ready')
            with self.assertRaisesRegex(TigerPaperError, 'real accounts'):
                _connect_paper(self.config)
            cls.assert_not_called()

    def test_server_paper_confirmation_is_mandatory_even_with_17_digit_id(self):
        self.client.get_managed_accounts.return_value[0].account_type = 'STANDARD'
        with patch('platforms.brokers.tiger_paper._paper_client_class', return_value=Mock(return_value=self.client)):
            with self.assertRaisesRegex(TigerPaperError, 'confirm'):
                _connect_paper(self.config)
        self.client.get_prime_assets.assert_not_called()

    def test_gateway_guard_blocks_real_foreign_and_unverified_requests(self):
        from tigeropen.trade.trade_client import TradeClient
        from tigeropen.common.request import OpenApiRequest
        cls = _paper_client_class()
        client = cls.__new__(cls)
        client._paper_account, client._paper_verified = ACCOUNT, False
        with patch.object(TradeClient, 'execute', return_value='{}') as execute:
            request = lambda method, account=ACCOUNT: OpenApiRequest(method, NS(account=account))
            client.execute(request('accounts'))
            for method in ('prime_assets', 'place_order', 'cancel_order'):
                with self.assertRaises(TigerPaperError):
                    client.execute(request(method))
            client._paper_verified = True
            for method in ('place_order', 'cancel_order', 'orders'):
                client.execute(request(method))
                with self.assertRaises(TigerPaperError):
                    client.execute(request(method, '12345678'))
            for method in ('transfer', 'modify_order', 'user_token_refresh'):
                with self.assertRaises(TigerPaperError):
                    client.execute(request(method))
            with self.assertRaises(TigerPaperError):
                client.execute(request('orders'), url='https://invalid.example/gateway')
            client._paper_account = '12345678'
            with self.assertRaises(TigerPaperError):
                client.execute(request('place_order', '12345678'))
            self.assertEqual(execute.call_count, 4)

    def test_cash_is_usd_available_cash_never_margin_or_converted_hkd(self):
        account = self.broker.account()
        self.assertEqual(account.cash, D(9000))
        self.assertNotIn(ACCOUNT, account.account_id)
        self.client.get_prime_assets.return_value.segments['S'].currency_assets['USD'].cash_balance = float('inf')
        with self.assertRaises(TigerPaperError):
            self.broker.account()

    def test_foreign_currency_short_fractional_and_foreign_account_positions_block(self):
        for currency, qty, account in [('HKD', 1, ACCOUNT), ('USD', -1, ACCOUNT),
                                      ('USD', 0.5, ACCOUNT), ('USD', 1, 'FOREIGN')]:
            self.client.get_positions.return_value = [NS(account=account,
                contract=NS(sec_type='STK', currency=currency, symbol='AAPL'), quantity=qty)]
            with self.assertRaises(TigerPaperError):
                self.broker.account()

    def test_submit_pending_partial_filled_and_restart_never_resubmits(self):
        first = self.broker.submit(self.intent)
        self.assertEqual(first.status, 'accepted')
        submitted = self.client.place_order.call_args.kwargs['order']
        self.assertEqual(submitted.account, ACCOUNT)
        self.assertEqual(submitted.time_in_force, 'DAY')
        self.assertFalse(submitted.outside_rth)
        remote = self.remotes[1]
        remote.status, remote.filled, remote.avg_fill_price = 'PartiallyFilled', 2, 99
        self.assertEqual(self.broker.query(self.intent.client_id).status, 'partial')
        self.broker.close()
        self.broker = TigerPaperBroker(self.client, ACCOUNT, self.path, clock=lambda: NOW)
        remote.status, remote.filled = 'Filled', 10
        self.assertEqual(self.broker.submit(self.intent).status, 'filled')
        self.assertEqual(self.client.place_order.call_count, 1)
        with self.assertRaisesRegex(TigerPaperError, 'reused'):
            self.broker.submit(replace(self.intent, quantity=D(11)))

    def test_lost_ack_recovers_by_tag_without_resubmitting(self):
        place = self.client.place_order.side_effect
        def lost(**kwargs):
            place(**kwargs)
            raise ConnectionError('SECRET signed request contents')
        self.client.place_order.side_effect = lost
        with self.assertRaises(TigerPaperError) as error:
            self.broker.submit(self.intent)
        self.assertNotIn('SECRET', str(error.exception))
        self.client.get_orders.return_value.result = list(self.remotes.values())
        self.assertEqual(self.broker.submit(self.intent).status, 'accepted')
        self.assertEqual(self.client.place_order.call_count, 1)

    def test_unknown_transmission_stays_unknown_never_retried(self):
        self.client.place_order.side_effect = TimeoutError('SECRET')
        with self.assertRaises(TigerPaperError):
            self.broker.submit(self.intent)
        self.assertEqual(self.broker.submit(self.intent).status, 'unknown')
        self.assertEqual(self.client.place_order.call_count, 1)
        with self.assertRaisesRegex(TigerPaperError, 'unresolved'):
            self.broker.submit(replace(self.intent, client_id='different-order'))
        self.assertEqual(self.client.place_order.call_count, 1)

    def test_cancellation_ack_does_not_imply_cancelled(self):
        first = self.broker.submit(self.intent)
        self.client.cancel_order.return_value = 1
        self.assertEqual(self.broker.cancel(first.broker_id).status, 'accepted')
        self.remotes[1].status = 'Cancelled'
        self.assertEqual(self.broker.cancel(first.broker_id).status, 'cancelled')
        self.assertEqual(self.client.cancel_order.call_count, 1)
        with self.assertRaisesRegex(TigerPaperError, 'owned'):
            self.broker.cancel('999')

    def test_returned_identity_and_fill_regressions_are_rejected(self):
        self.broker.submit(self.intent)
        remote = self.remotes[1]
        for name, bad in [('account', 'FOREIGN'), ('quantity', 11), ('limit_price', 99),
                          ('user_mark', 'foreign'), ('filled_scale', 1)]:
            good = getattr(remote, name)
            setattr(remote, name, bad)
            with self.assertRaises(TigerPaperError):
                self.broker.query(self.intent.client_id)
            setattr(remote, name, good)
        remote.status, remote.filled, remote.avg_fill_price = 'PartiallyFilled', 2, 99
        self.broker.query(self.intent.client_id)
        remote.filled = 1
        with self.assertRaises(TigerPaperError):
            self.broker.query(self.intent.client_id)

    def test_bad_orders_are_blocked_before_transmission(self):
        for intent in (replace(self.intent, quantity=D('0.5')), replace(self.intent, side='SELL'),
                       replace(self.intent, quantity=D(2000)), replace(self.intent, symbol='DEMO-03'),
                       replace(self.intent, limit_price=D(-1))):
            with self.assertRaises((TigerPaperError, ValueError)):
                self.broker.submit(intent)
        self.client.place_order.assert_not_called()

    def test_open_orders_from_later_page_block_submission(self):
        self.client.get_orders.side_effect = [NS(result=[], next_page_token='page-2'),
            NS(result=[NS(account=ACCOUNT, id=3, status='PendingCancel')], next_page_token=None)]
        with self.assertRaisesRegex(TigerPaperError, 'unresolved'):
            self.broker.submit(self.intent)
        self.client.place_order.assert_not_called()
        self.assertEqual(self.client.get_orders.call_args.kwargs['page_token'], 'page-2')

    def test_ambiguous_or_cycling_pagination_fails_closed(self):
        self.client.get_orders.return_value = NS(result=[], next_page_token='same')
        with self.assertRaisesRegex(TigerPaperError, 'advance'):
            _orders(self.client, ACCOUNT)
        self.client.get_orders.return_value = NS(result=[NS(account=ACCOUNT, id=i+1) for i in range(300)], next_page_token=None)
        with self.assertRaisesRegex(TigerPaperError, 'ambiguous'):
            _orders(self.client, ACCOUNT)

    def test_wrong_account_ledger_and_missing_ledger_are_not_recreated(self):
        other = fixture_client()
        other._paper_account = '20261001000000002'
        with self.assertRaisesRegex(TigerPaperError, 'ledger'):
            TigerPaperBroker(other, other._paper_account, self.path)
        with self.assertRaisesRegex(TigerPaperError, 'missing'):
            TigerPaperBroker(self.client, ACCOUNT, self.root / 'missing.sqlite')

    def test_inspection_is_read_only_and_confirms_paper(self):
        with patch('platforms.brokers.tiger_paper._connect_paper', return_value=(self.client, ACCOUNT)):
            result = self.connector.inspect(connect=True)
        self.assertEqual(result['account_type'], 'PAPER')
        self.assertEqual(result['cash'], '9000')
        self.assertNotIn(ACCOUNT, json.dumps(result))
        self.client.place_order.assert_not_called()
        self.client.cancel_order.assert_not_called()

    def test_real_sdk_status_enums_are_mapped(self):
        from tigeropen.common.consts import OrderStatus
        for value, expected in [(OrderStatus.HELD, 'accepted'), (OrderStatus.FILLED, 'filled'),
                                (OrderStatus.PARTIALLY_FILLED, 'partial'), (OrderStatus.PENDING_CANCEL, 'pending')]:
            self.assertEqual(_status(NS(status=value)), expected)

    def test_stable_asset_id_is_mapped_and_order_ticker_survives_restart(self):
        self.broker.symbol_map = {'AAPL-STABLE-IDENTIFIER': 'AAPL'}
        intent = replace(self.intent, symbol='AAPL-STABLE-IDENTIFIER')
        self.broker.submit(intent)
        self.assertEqual(self.client.place_order.call_args.kwargs['order'].contract.symbol, 'AAPL')
        self.client.get_positions.return_value = [NS(account=ACCOUNT,
            contract=NS(sec_type='STK', currency='USD', symbol='AAPL'), quantity=2)]
        self.assertEqual(self.broker.account().positions, (('AAPL-STABLE-IDENTIFIER', D(2)),))
        self.broker.close()
        self.broker = TigerPaperBroker(self.client, ACCOUNT, self.path, clock=lambda: NOW)
        self.assertEqual(self.broker.query(intent.client_id).status, 'accepted')
        self.assertEqual(self.client.place_order.call_count, 1)

    def test_unmapped_held_stock_blocks_execution(self):
        self.broker.symbol_map = {'OTHER-SID': 'MSFT'}
        self.client.get_positions.return_value = [NS(account=ACCOUNT,
            contract=NS(sec_type='STK', currency='USD', symbol='AAPL'), quantity=2)]
        with self.assertRaisesRegex(TigerPaperError, 'every held'):
            self.broker.account()

    def test_executor_uses_wall_clock_and_pauses_pending_without_repeat(self):
        journal = Journal(self.state / 'journal.sqlite')
        self.addCleanup(journal.close)
        policy = ExecutionPolicy(max_gross=D('.9'), max_weight=D('.45'))
        executor = Executor(self.broker, journal, policy, self.broker.fees)
        target = decision((('AAPL', .45),), day='2026-10-01')
        quotes = {'AAPL': Quote('AAPL', D(99), D(100), NOW)}
        with self.assertRaisesRegex(ExecutionBlocked, 'accepted'):
            executor.run(target, quotes, NOW - timedelta(days=1))
        self.assertEqual(self.client.place_order.call_count, 1)
        with self.assertRaisesRegex(ExecutionBlocked, 'accepted'):
            executor.run(target, quotes, NOW)
        self.assertEqual(self.client.place_order.call_count, 1)
        remote = self.remotes[1]
        remote.status, remote.filled, remote.avg_fill_price = 'Filled', remote.quantity, 100
        self.assertEqual(executor.run(target, quotes, NOW)['status'], 'complete')
        self.assertEqual(self.client.place_order.call_count, 1)

    def test_old_quotes_and_old_decision_cannot_be_replayed_to_tiger(self):
        journal = Journal(self.state / 'journal.sqlite')
        self.addCleanup(journal.close)
        executor = Executor(self.broker, journal, ExecutionPolicy(), self.broker.fees)
        quotes = {'AAPL': Quote('AAPL', D(99), D(100), NOW - timedelta(hours=1))}
        with self.assertRaisesRegex(ValueError, 'Stale'):
            executor.run(decision((('AAPL', .19),), day='2026-10-01'), quotes, NOW - timedelta(hours=1))
        with self.assertRaisesRegex(ExecutionBlocked, 'another execution'):
            executor.run(decision((('AAPL', .19),), day='2026-09-01'), quotes, NOW)
        self.client.place_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
