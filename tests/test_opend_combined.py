"""US combined paper accounts stay simulation-only and cash financed."""
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal as D
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from framework.contracts import OrderIntent
from framework.trading import Fees
from platforms.opend import OpenDError, connection, load_sdk, select_account, settings
from platforms.brokers.futu_paper import FutuPaperConnector
from platforms.brokers.moomoo_paper import MoomooPaperConnector
from platforms.brokers.opend_broker import OpenDBroker, _cash_balance
from test_opend import NOW, Table, account_row
from test_opend_live import SDK


def combined_row(**changes):
    return account_row(**dict(dict(sim_acc_type='STOCK_AND_OPTION', acc_type='MARGIN'), **changes))


class CombinedSelectionTests(unittest.TestCase):
    def test_combined_us_account_is_selected_while_real_options_and_futures_are_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            for provider in ('futu', 'moomoo'):
                cfg = settings(provider, folder)
                rows = [combined_row(acc_id=1, trd_env='REAL'), combined_row(),
                        combined_row(acc_id=2, sim_acc_type='FUTURES'),
                        combined_row(acc_id=3, sim_acc_type='OPTION')]
                self.assertEqual(select_account(rows, cfg, SDK)[0], '123')
                for change in ({'acc_status': 'DISABLED'}, {'trdmarket_auth': ['HK']},
                               {'trdmarket_auth': ['US', 'HK']}, {'acc_type': 'UNKNOWN'},
                               {'security_firm': 'FUTUSG'}):
                    with self.subTest(provider=provider, change=change), self.assertRaises(OpenDError):
                        select_account([combined_row(**change)], cfg, SDK)

    def test_multiple_stock_capable_accounts_require_an_explicit_id(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = settings('futu', folder)
            rows = [combined_row(), account_row(acc_id=456)]
            with self.assertRaises(OpenDError):
                select_account(rows, cfg, SDK)
            self.assertEqual(select_account(rows, replace(cfg, account_id='123'), SDK)[0], '123')

    @unittest.skipUnless(all(importlib.util.find_spec(p) for p in ('futu', 'moomoo')), 'Install optional SDKs')
    def test_sdk_connection_carries_verified_paper_type_and_market_without_enabling_orders(self):
        with tempfile.TemporaryDirectory() as folder:
            for provider in ('futu', 'moomoo'):
                client = Mock()
                client.get_login_user_id.return_value = 111
                client.get_acc_list.return_value = (0, Table([combined_row()]))
                with patch('platforms.opend.socket.create_connection'), \
                        patch('platforms.opend.trade_context_class', return_value=Mock(return_value=client)):
                    with connection(settings(provider, folder), trade=True) as (sdk, _, profile):
                        self.assertEqual(sdk.SimAccType.STOCK_AND_OPTION, profile['sim_acc_type'])
                        self.assertEqual(profile['trdmarket_auth'], ('US',))
                        self.assertEqual(profile['environment'], 'SIMULATE')
                        self.assertFalse(client._allow_orders)
                client.close.assert_called_once()


class CombinedPaperCases:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.profile = dict(provider=self.provider, user_id='111', account_id='123',
                            environment='SIMULATE', reported_firm='N/A',
                            sim_acc_type='STOCK_AND_OPTION', trdmarket_auth=('US',))
        self.trade, self.quotes = Mock(), Mock()
        self.trade._verified_account, self.trade._trading_environment = '123', 'SIMULATE'
        self.trade._allow_orders = True
        self.funds = dict(us_cash=10000, usd_net_cash_power='N/A', frozen_cash=500,
                          currency='N/A', cash=9999999, power=2000000)
        self.trade.accinfo_query.side_effect = lambda **kw: (0, Table([self.funds]))
        self.trade.position_list_query.return_value = (0, Table([]))
        self.trade.history_order_list_query.return_value = (0, Table([]))
        self.remote, self.connections = [], []
        self.trade.order_list_query.side_effect = lambda **kw: (0, Table(self.remote))
        def place(**kw):
            self.remote.append(dict(kw, order_id='456', order_status='SUBMITTED',
                                    dealt_qty=0, dealt_avg_price=0, currency='USD'))
            return 0, Table([dict(order_id='456')])
        self.trade.place_order.side_effect = place
        self.quotes.get_stock_basicinfo.side_effect = lambda **kw: (0, Table([
            dict(code=code, stock_type='STOCK', delisting=False) for code in kw['code_list']]))
        self.quote_login = '111'
        @contextmanager
        def fake_connection(cfg, **kwargs):
            self.assertEqual(cfg.environment, 'SIMULATE')
            self.connections.append(kwargs)
            client = self.trade if kwargs.get('trade') else self.quotes
            profile = dict(self.profile, user_id='111' if kwargs.get('trade') else self.quote_login)
            try:
                yield SDK, client, profile
            finally:
                client.close()
        patcher = patch('platforms.brokers.opend_broker.connection', fake_connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        cls = FutuPaperConnector if self.provider == 'futu' else MoomooPaperConnector
        self.connector = cls(config_dir=str(self.folder), state_dir=str(self.folder))
        self.connector.initialize(self.folder, 1)
        self.broker = self.connector.connect(self.folder, None, Fees())
        self.broker.clock = lambda: NOW
        self.addCleanup(self.broker.close)
        self.intent = OrderIntent('combined-test-1', 'AAPL', 'BUY', D(10), D(100))

    def test_cash_excludes_frozen_money_and_margin_and_clamps_to_zero(self):
        self.assertEqual(self.broker.account().cash, D(9500))
        self.funds['frozen_cash'] = 20000
        self.assertEqual(self.broker.account().cash, D(0))
        self.funds['frozen_cash'] = 500
        self.funds['usd_net_cash_power'] = 7000
        self.assertEqual(self.broker.account().cash, D(7000))
        self.assertEqual(self.trade.accinfo_query.call_args.kwargs['trd_env'], 'SIMULATE')

    def test_missing_cash_power_fallback_is_limited_to_us_combined_paper(self):
        for changes in ({'environment': 'REAL'}, {'sim_acc_type': 'STOCK'},
                        {'trdmarket_auth': ('US', 'HK')}, {'trdmarket_auth': ()}):
            with self.subTest(changes=changes), self.assertRaises(OpenDError):
                _cash_balance(self.funds, dict(self.profile, **changes))
        for changes in ({'frozen_cash': None}, {'frozen_cash': -1}, {'frozen_cash': 'NaN'},
                        {'usd_net_cash_power': 'NaN'}, {'us_cash': -1}):
            with self.subTest(changes=changes), self.assertRaises(OpenDError):
                _cash_balance(dict(self.funds, **changes), self.profile)

    def test_stock_order_is_paper_scoped_and_never_unlocks_real_trading(self):
        self.assertEqual(self.broker.submit(self.intent).status, 'accepted')
        request = self.trade.place_order.call_args.kwargs
        self.assertEqual((request['trd_env'], request['acc_id']), ('SIMULATE', 123))
        self.assertEqual(self.broker.mode, 'broker_paper')
        self.assertFalse(self.broker.live_enabled)
        self.trade.unlock_trade.assert_not_called()
        self.quotes.get_stock_basicinfo.assert_called_once()

    def test_derivatives_or_missing_metadata_stop_before_any_transmission(self):
        self.quotes.get_stock_basicinfo.side_effect = None
        for result in ((0, Table([dict(code='US.AAPL', stock_type='DRVT', delisting=False)])),
                       (0, Table([])), (-1, 'PRIVATE')):
            self.quotes.get_stock_basicinfo.return_value = result
            with self.assertRaises(OpenDError):
                self.broker.submit(self.intent)
        self.trade.place_order.assert_not_called()
        self.assertEqual(self.broker.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)

    def test_existing_option_holdings_and_borrowed_buying_power_are_rejected(self):
        self.trade.position_list_query.return_value = (0, Table([dict(code='US.F260101C1', qty=1,
            can_sell_qty=1, position_side='LONG', currency='USD')]))
        self.quotes.get_stock_basicinfo.side_effect = lambda **kw: (0, Table([
            dict(code=code, stock_type='DRVT', delisting=False) for code in kw['code_list']]))
        with self.assertRaises(OpenDError):
            self.broker.account()
        self.trade.position_list_query.return_value = (0, Table([]))
        self.quotes.get_stock_basicinfo.side_effect = lambda **kw: (0, Table([
            dict(code=code, stock_type='STOCK', delisting=False) for code in kw['code_list']]))
        self.funds.update(us_cash=1500, frozen_cash=600, power=9999999)
        with self.assertRaisesRegex(OpenDError, 'Insufficient'):
            self.broker.submit(self.intent)
        self.trade.place_order.assert_not_called()

    def test_validator_is_mandatory_and_quote_login_must_match_trade_login(self):
        with self.assertRaisesRegex(OpenDError, 'instrument validation'):
            OpenDBroker(SDK, self.trade, self.profile, self.folder / self.connector.state_files[0])
        self.quote_login = '222'
        with self.assertRaisesRegex(OpenDError, 'same login'):
            self.connector.connect(self.folder, None, Fees())
        self.trade.place_order.assert_not_called()

    def test_read_only_diagnostics_close_both_contexts_and_do_not_trade(self):
        self.trade.close.reset_mock()
        self.quotes.close.reset_mock()
        with patch('platforms.brokers.opend_broker.configuration_check', return_value={'issues': []}):
            result = self.connector.inspect(connect=True)
        self.assertEqual(result['status'], 'connected')
        self.assertFalse(self.connections[-2]['allow_orders'])
        self.trade.place_order.assert_not_called()
        self.trade.modify_order.assert_not_called()
        self.trade.unlock_trade.assert_not_called()
        self.trade.close.assert_called_once()
        self.quotes.close.assert_called_once()


class FutuCombinedPaperTests(CombinedPaperCases, unittest.TestCase):
    provider = 'futu'


class MoomooCombinedPaperTests(CombinedPaperCases, unittest.TestCase):
    provider = 'moomoo'
