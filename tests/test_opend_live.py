"""Real-account protocol contracts with all network activity replaced by test doubles."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from hashlib import md5, sha256
import importlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import bot
from assembly import _build_platform, open_broker_paper_session, open_live_session, open_paper_session
from framework.configuration import load_config, normalize_config
from framework.contracts import OrderIntent
from framework.execution import Executor, ExecutionBlocked
from framework.journal import Journal, packed
from framework.trading import Fees, ExecutionPolicy, Quote
from platforms.opend import ROOT, PROFILES, OpenDError, configuration_check, connection, load_sdk, select_account, settings, trade_context_class
from platforms.brokers.opend_broker import OpenDBroker, _identity, instrument_validator
from platforms.brokers.futu_live import FutuLiveConnector
from platforms.brokers.moomoo_live import MoomooLiveConnector
from test_opend import SDK as PAPER_SDK, Table, NOW, account_row
from test_execution import decision

SDK = NS(**vars(PAPER_SDK), TrdAccType=NS(CASH='CASH', MARGIN='MARGIN'),
         TrdAccRole=NS(NORMAL='NORMAL'), Market=NS(US='US'), SecurityType=NS(STOCK='STOCK', ETF='ETF'))


def live_row(provider='futu', **kwargs):
    return account_row(**dict(dict(trd_env='REAL', acc_type='CASH', acc_role='NORMAL',
        sim_acc_type='N/A', security_firm=PROFILES[provider]['firm']), **kwargs))


class LiveSelectionTests(unittest.TestCase):
    def test_all_four_configs_share_the_strategy_but_have_separate_ledgers(self):
        from tools.build_quantconnect import build
        baseline = normalize_config(load_config(ROOT / 'portfolio_config.json'))
        directories = set()
        for p in ('futu', 'moomoo'):
            for mode in ('paper', 'live'):
                cfg = normalize_config(load_config(ROOT / (p + '_' + mode + '_config.json')))
                self.assertEqual(cfg['strategy'], baseline['strategy'])
                self.assertEqual(build(cfg), build(baseline))
                connector = _build_platform(cfg['broker'], 'broker')
                self.assertEqual(connector.environment, 'REAL' if mode == 'live' else 'SIMULATE')
                directories.add(connector.state_dir)
        self.assertEqual(len(directories), 4)

    def test_real_selection_requires_explicit_login_account_region_and_us_auth(self):
        for provider in ('futu', 'moomoo'):
            cfg = replace(settings(provider, environment='REAL'), account_id='123', login_user_id='111')
            self.assertEqual(select_account([account_row(), live_row(provider)], cfg, SDK)[0], '123')
            for changes in ({'trd_env': 'SIMULATE'}, {'acc_id': 456}, {'acc_role': 'MASTER'},
                            {'acc_type': 'DERIVATIVES'}, {'security_firm': 'N/A'},
                            {'security_firm': 'FUTUINC'}, {'trdmarket_auth': ['HK']}, {'acc_status': 'DISABLED'}):
                with self.subTest(provider=provider, changes=changes), self.assertRaises(OpenDError):
                    select_account([live_row(provider, **changes)], cfg, SDK)
            for changes in ({'account_id': None}, {'login_user_id': None}):
                with self.assertRaises(OpenDError):
                    select_account([live_row(provider)], replace(cfg, **changes), SDK)
            with self.assertRaises(OpenDError):
                select_account([live_row(provider), live_row(provider)], cfg, SDK)

    def test_missing_live_ids_and_activation_fail_before_sdk_or_socket(self):
        with patch('platforms.opend.load_sdk') as sdk, patch('platforms.opend.socket.create_connection') as socket:
            cfg = settings('futu', environment='REAL')
            for target, kwargs in ((cfg, {}), (replace(cfg, account_id='123', login_user_id='111'), {'allow_orders': True})):
                with self.assertRaises(OpenDError):
                    with connection(target, trade=True, **kwargs):
                        self.fail('connected')
            sdk.assert_not_called()
            socket.assert_not_called()

    def test_live_and_paper_commands_reject_the_other_config_before_network_or_state(self):
        with tempfile.TemporaryDirectory() as folder, patch('platforms.brokers.opend_broker.connection') as connect:
            paper = normalize_config(load_config(ROOT / 'futu_paper_config.json'))
            live = normalize_config(load_config(ROOT / 'futu_live_config.json'))
            target = Path(folder) / 'new'
            for manager in (open_paper_session(live, None, target, None),
                            open_broker_paper_session(live, None, target, None),
                            open_live_session(paper, None, target, None, enable_live=True),
                            open_live_session(live, None, target, None)):
                with self.assertRaises(ValueError):
                    with manager:
                        self.fail('opened wrong mode')
            self.assertFalse(target.exists())
            connect.assert_not_called()

    def test_cli_without_activation_never_reads_snapshot_connects_or_prompts(self):
        with patch('sys.argv', ['bot.py', 'broker-live', '--config', 'futu_live_config.json',
                               '--snapshot', 'does-not-exist.json', '--unlock']), patch.object(bot, 'run_live') as run, \
                patch('getpass.getpass') as prompt, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                bot.main()
            self.assertEqual(caught.exception.code, 2)
            run.assert_not_called()
            prompt.assert_not_called()
        with patch.object(bot, 'prepare_portfolio') as prepare, self.assertRaises(ValueError):
            bot.run_live('missing', 'missing')
        prepare.assert_not_called()

    def test_paper_ledger_identity_is_backwards_compatible_and_live_is_distinct(self):
        profile = dict(provider='futu', user_id='111', account_id='123')
        original = sha256(packed(['futu', '111', '123']).encode()).hexdigest()
        self.assertEqual(_identity(profile), original)
        self.assertEqual(_identity(dict(profile, environment='SIMULATE')), original)
        self.assertNotEqual(_identity(dict(profile, environment='REAL')), original)

    def test_password_prompt_never_falls_back_to_echoing_input(self):
        import getpass
        import warnings
        runner, broker, journal = Mock(), Mock(), Mock()
        @contextmanager
        def session(*args, **kwargs):
            yield broker, journal
        def unsafe_prompt(*args):
            warnings.warn('echo would be enabled', getpass.GetPassWarning)
            self.fail('Password input continued without echo protection')
        with patch.object(bot, 'prepare_portfolio', return_value=({}, runner, object(), object())), \
                patch.object(bot, 'open_live_session', session), patch('getpass.getpass', side_effect=unsafe_prompt):
            with self.assertRaisesRegex(OpenDError, 'Secure password input'):
                bot.run_live('snapshot', 'config', enable_live=True, unlock=True)
        broker.unlock.assert_not_called()
        runner.execute.assert_not_called()

    def test_live_status_is_offline_by_default_and_explains_unconfigured_ids(self):
        for cls in (FutuLiveConnector, MoomooLiveConnector):
            with tempfile.TemporaryDirectory() as folder, patch('platforms.brokers.opend_broker.connection') as connect:
                result = cls(config_dir=folder).inspect()
                self.assertEqual(result['account_environment'], 'REAL')
                self.assertFalse(result['network_requested'])
                self.assertTrue(result['issues'])
                connect.assert_not_called()


class LiveBrokerCases:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        (self.folder / 'connection.json').write_text(json.dumps({'account_id': '123', 'login_user_id': '111'}))
        self.profile = dict(provider=self.provider, user_id='111', account_id='123', environment='REAL',
                            reported_firm=PROFILES[self.provider]['firm'])
        self.trade, self.quotes = Mock(), Mock()
        self.trade._verified_account, self.trade._trading_environment = '123', 'REAL'
        self.trade._allow_orders = True
        self.trade.accinfo_query.return_value = (0, Table([dict(us_cash=10000, usd_net_cash_power=9000, power=9999999)]))
        self.trade.position_list_query.return_value = (0, Table([]))
        self.trade.unlock_trade.return_value = (0, None)
        self.remote = {}
        self.trade.order_list_query.side_effect = lambda **kw: (0, Table([
            r for oid, r in self.remote.items() if not kw.get('order_id') or kw['order_id'] == oid]))
        self.trade.history_order_list_query.return_value = (0, Table([]))
        self.trade.place_order.side_effect = self.place
        self.trade.modify_order.side_effect = self.cancel
        self.quotes.get_stock_basicinfo.side_effect = lambda **kw: (0, Table([
            dict(code=code, stock_type='STOCK', delisting=False) for code in kw['code_list']]))
        self.connections = []
        @contextmanager
        def fake_connection(cfg, **kwargs):
            self.assertEqual(cfg.environment, 'REAL')
            self.connections.append(kwargs)
            client = self.trade if kwargs.get('trade') else self.quotes
            try:
                yield SDK, client, dict(self.profile)
            finally:
                client.close()
        self.connection_patch = patch('platforms.brokers.opend_broker.connection', fake_connection)
        self.connection_patch.start()
        self.addCleanup(self.connection_patch.stop)
        cls = FutuLiveConnector if self.provider == 'futu' else MoomooLiveConnector
        self.connector = cls(config_dir=str(self.folder), state_dir=str(self.folder))
        self.connector.initialize(self.folder, 99999999)
        self.broker = self.connector.connect(self.folder, None, Fees(), enable_live=True)
        self.broker.clock = lambda: NOW
        self.addCleanup(self.broker.close)
        self.intent = OrderIntent('live-test-1', 'AAPL', 'BUY', D(10), D(100))

    def place(self, **kw):
        self.assertEqual((kw['trd_env'], kw['acc_id']), ('REAL', 123))
        oid = str(len(self.remote) + 1)
        self.remote[oid] = dict(kw, order_id=oid, order_status='SUBMITTED', dealt_qty=0,
                                dealt_avg_price=0, currency='USD')
        return 0, Table([dict(order_id=oid)])

    def cancel(self, **kw):
        self.remote[kw['order_id']]['order_status'] = 'CANCELLING_ALL'
        return 0, Table([dict(order_id=kw['order_id'])])

    def test_live_reads_and_writes_use_real_scope_and_do_not_auto_unlock(self):
        self.assertEqual(self.broker.mode, 'live')
        self.assertTrue(self.broker.live_enabled)
        self.assertEqual(self.broker.account().cash, D(9000))
        self.assertEqual(self.broker.submit(self.intent).status, 'accepted')
        self.trade.unlock_trade.assert_not_called()
        self.assertEqual(self.trade.accinfo_query.call_args.kwargs['trd_env'], 'REAL')
        self.assertEqual(self.trade.position_list_query.call_args.kwargs['acc_id'], 123)
        self.assertIn('-live-', self.trade.place_order.call_args.kwargs['remark'])
        self.assertTrue(any(c.get('enable_live') is True for c in self.connections))

    def test_live_requires_enable_even_if_the_gateway_is_already_unlocked(self):
        count = len(self.connections)
        with self.assertRaises(OpenDError):
            self.connector.connect(self.folder, None, Fees())
        self.assertEqual(len(self.connections), count)
        self.trade.place_order.assert_not_called()

    def test_lost_live_acknowledgement_recovers_without_duplicate_order(self):
        def lost(**kw):
            self.place(**kw)
            raise TimeoutError('PRIVATE ACCOUNT DETAILS')
        self.trade.place_order.side_effect = lost
        self.assertEqual(self.broker.submit(self.intent).status, 'unknown')
        restarted = self.connector.connect(self.folder, None, Fees(), enable_live=True)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.submit(self.intent).status, 'accepted')
        self.trade.place_order.assert_called_once()

    def test_unknown_live_transmission_blocks_later_orders_and_stays_unknown(self):
        self.trade.place_order.side_effect = TimeoutError('PRIVATE')
        self.assertEqual(self.broker.submit(self.intent).status, 'unknown')
        self.assertEqual(self.broker.submit(self.intent).status, 'unknown')
        with self.assertRaises(OpenDError):
            self.broker.submit(replace(self.intent, client_id='next'))
        self.trade.place_order.assert_called_once()

    def test_partial_fill_and_cancel_ack_do_not_claim_final_cancellation(self):
        self.broker.submit(self.intent)
        self.remote['1'].update(order_status='FILLED_PART', dealt_qty=2, dealt_avg_price=99)
        self.assertEqual(self.broker.query(self.intent.client_id).filled_quantity, D(2))
        self.assertEqual(self.broker.cancel('1').status, 'pending')
        self.assertTrue(self.broker.open_orders())
        self.remote['1']['order_status'] = 'CANCELLED_PART'
        self.assertEqual(self.broker.query(self.intent.client_id).status, 'cancelled')
        self.assertEqual(self.trade.modify_order.call_args.kwargs['trd_env'], 'REAL')

    def test_derivatives_and_unavailable_security_metadata_block_before_send(self):
        for reply in ((0, Table([dict(code='US.AAPL', stock_type='DRVT', delisting=False)])),
                      (0, Table([])), (-1, 'PRIVATE unavailable')):
            self.quotes.get_stock_basicinfo.side_effect = None
            self.quotes.get_stock_basicinfo.return_value = reply
            with self.assertRaises(OpenDError):
                self.broker.submit(self.intent)
        self.trade.place_order.assert_not_called()
        self.assertEqual(self.broker.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)

    def test_live_holdings_are_also_validated_and_only_cash_financed_buys_are_allowed(self):
        self.trade.position_list_query.return_value = (0, Table([dict(code='US.F260101C1', qty=1, can_sell_qty=1,
            position_side='LONG', currency='USD', acc_id=123)]))
        self.quotes.get_stock_basicinfo.side_effect = lambda **kw: (0, Table([
            dict(code=code, stock_type='DRVT', delisting=False) for code in kw['code_list']]))
        with self.assertRaises(OpenDError):
            self.broker.account()
        self.trade.position_list_query.return_value = (0, Table([]))
        self.quotes.get_stock_basicinfo.side_effect = lambda **kw: (0, Table([
            dict(code=code, stock_type='STOCK', delisting=False) for code in kw['code_list']]))
        self.trade.accinfo_query.return_value = (0, Table([dict(us_cash=100, usd_net_cash_power=100, power=1000000)]))
        with self.assertRaisesRegex(OpenDError, 'Insufficient'):
            self.broker.submit(self.intent)
        self.trade.place_order.assert_not_called()

    def test_shared_executor_uses_wall_clock_and_recovers_an_accepted_live_order(self):
        journal = Journal(self.folder / 'test-journal.sqlite')
        self.addCleanup(journal.close)
        executor = Executor(self.broker, journal, ExecutionPolicy(), Fees())
        target = decision((('AAPL', .1),), day='2026-10-01')
        stale = NOW - timedelta(minutes=10)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            executor.run(target, {'AAPL': Quote('AAPL', D(99), D(100), stale)}, stale)
        self.trade.place_order.assert_not_called()
        quotes = {'AAPL': Quote('AAPL', D(99), D(100), NOW)}
        with self.assertRaisesRegex(ExecutionBlocked, 'accepted'):
            executor.run(target, quotes, NOW)
        remote = self.remote['1']
        remote.update(order_status='FILLED_ALL', dealt_qty=remote['qty'], dealt_avg_price=99)
        self.assertEqual(executor.run(target, quotes, NOW)['status'], 'complete')
        self.trade.place_order.assert_called_once()

    def test_live_and_paper_ledgers_are_not_interchangeable(self):
        path = self.folder / self.connector.state_files[0]
        paper_profile = dict(self.profile, environment='SIMULATE')
        self.trade._trading_environment = 'SIMULATE'
        with self.assertRaisesRegex(OpenDError, 'mismatched'):
            OpenDBroker(SDK, self.trade, paper_profile, path)

    def test_unlock_is_explicit_and_does_not_persist_password(self):
        password = 'OFFLINE-TEST-ONLY'
        self.broker.unlock(password)
        self.trade.unlock_trade.assert_called_once_with(password=password)
        for row in self.broker.db.iterdump():
            self.assertNotIn(password, row)
        self.assertNotIn(password, repr(vars(self.broker)))

    def test_account_diagnostics_never_enable_orders_or_unlock(self):
        with patch('platforms.brokers.opend_broker.configuration_check', return_value={'issues': [], 'network_requested': False}):
            self.assertEqual(self.connector.inspect(connect=True)['status'], 'connected')
        self.assertFalse(self.connections[-2]['allow_orders'])
        self.trade.place_order.assert_not_called()
        self.trade.unlock_trade.assert_not_called()

    def test_both_connections_close_when_the_order_session_closes(self):
        self.trade.close.reset_mock()
        self.quotes.close.reset_mock()
        self.broker.close()
        self.broker.close()
        self.trade.close.assert_called_once()
        self.quotes.close.assert_called_once()


class FutuLiveTests(LiveBrokerCases, unittest.TestCase):
    provider = 'futu'


class MoomooLiveTests(LiveBrokerCases, unittest.TestCase):
    provider = 'moomoo'


@unittest.skipUnless(all(importlib.util.find_spec(p) for p in ('futu', 'moomoo')), 'Install optional SDKs')
class LiveSDKContracts(unittest.TestCase):
    def make_context(self, provider, allow_orders=True):
        sdk = load_sdk(provider)
        base = importlib.import_module(provider + '.common.open_context_base').OpenContextBase
        cls = trade_context_class(sdk, 'REAL')
        cls.close = lambda self, *a, **kw: None
        with patch.object(base, '__init__', return_value=None):
            ctx = cls(filter_trdmarket=sdk.TrdMarket.US, security_firm=PROFILES[provider]['firm'])
        ctx._verified_account, ctx._allow_orders, ctx._verified_firm = '123', allow_orders, PROFILES[provider]['firm']
        ctx.get_sync_conn_id = lambda: 0
        ctx._check_acc_id_and_acc_index = lambda env, acc, index: (0, '', acc, ['US'])
        ctx._check_acc_id = lambda env, acc: (0, '', acc)
        return sdk, base, ctx

    def test_real_sdk_order_and_cancel_methods_use_real_scope_and_correct_arguments(self):
        for provider in ('futu', 'moomoo'):
            sdk, base, ctx = self.make_context(provider)
            seen = []
            def factory(this, pack, unpack):
                def process(**kwargs):
                    inspect.signature(pack).bind(**kwargs)
                    seen.append((pack.__qualname__, kwargs))
                    return 0, '', '777' if pack.__qualname__.startswith('PlaceOrder.') else []
                return process
            with patch.object(base, '_get_sync_query_processor', factory):
                args = dict(price=100, qty=1, code='US.AAPL', trd_side='BUY', trd_env='REAL', acc_id=123, session='RTH')
                self.assertEqual(ctx.place_order(**args)[0], 0)
                self.assertEqual(ctx.modify_order('CANCEL', '777', 0, 0, trd_env='REAL', acc_id=123)[0], 0)
                before = len(seen)
                for changes in ({'trd_env': 'SIMULATE'}, {'acc_id': 456}, {'acc_id': 0}, {'session': 'ALL'}):
                    with self.assertRaises(OpenDError):
                        ctx.place_order(**dict(args, **changes))
                self.assertEqual(len(seen), before)
            self.assertTrue(all(kw['trd_env'] == 'REAL' and kw['acc_id'] == 123 for _, kw in seen))

    def test_unlock_does_not_cache_password_or_subscribe_other_accounts(self):
        for provider in ('futu', 'moomoo'):
            sdk, base, ctx = self.make_context(provider)
            password = 'OFFLINE-TEST-ONLY'
            processor = Mock(return_value=(0, '', None))
            ctx._async_sub_acc_push = Mock()
            with patch.object(base, '_get_sync_query_processor', return_value=processor):
                self.assertEqual(call_unlock(ctx, password), (0, None))
                kw = processor.call_args.kwargs
                self.assertEqual(kw['security_firm'], PROFILES[provider]['firm'])
                self.assertEqual(kw['password_md5'], md5(password.encode()).hexdigest())
                queries = importlib.import_module(provider + '.trade.trade_query')
                inspect.signature(queries.UnlockTrade.pack_req).bind(**kw)
                self.assertIsNone(ctx._ctx_unlock)
                self.assertNotIn(password, repr(vars(ctx)))
                ctx.on_api_socket_reconnected()
                processor.assert_called_once()
                ctx._async_sub_acc_push.assert_not_called()
                ctx._allow_orders = False
                with self.assertRaises(OpenDError):
                    call_unlock(ctx, password)

    def test_live_read_only_context_cannot_place_cancel_or_unlock(self):
        for provider in ('futu', 'moomoo'):
            _, base, ctx = self.make_context(provider, False)
            processor = Mock()
            with patch.object(base, '_get_sync_query_processor', return_value=processor):
                for action in (lambda: ctx.place_order(100, 1, 'US.AAPL', 'BUY', trd_env='REAL', acc_id=123, session='RTH'),
                               lambda: ctx.modify_order('CANCEL', '777', 0, 0, trd_env='REAL', acc_id=123),
                               lambda: call_unlock(ctx, 'OFFLINE-TEST-ONLY')):
                    with self.assertRaises(OpenDError):
                        action()
            processor.assert_not_called()

    def test_connection_verifies_real_profile_and_keeps_inspection_read_only(self):
        for provider in ('futu', 'moomoo'):
            cfg = replace(settings(provider, environment='REAL'), account_id='123', login_user_id='111')
            client = Mock()
            client.get_login_user_id.return_value = 111
            client.get_acc_list.return_value = (0, Table([account_row(), live_row(provider)]))
            with patch('platforms.opend.socket.create_connection'), \
                    patch('platforms.opend.trade_context_class', return_value=Mock(return_value=client)) as factory:
                with connection(cfg, trade=True) as (_, _, profile):
                    self.assertEqual(profile['environment'], 'REAL')
                    self.assertEqual(profile['account_id'], '123')
                    self.assertFalse(client._allow_orders)
                self.assertEqual(factory.call_args.args[1], 'REAL')
                client.close.assert_called_once()
                client.unlock_trade.assert_not_called()
                client.place_order.assert_not_called()


def call_unlock(ctx, password):
    return ctx.unlock_trade(password=password)
