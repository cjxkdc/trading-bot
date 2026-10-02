import importlib
import importlib.util
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from assembly import _build_platform, inspect_broker, inspect_market_data, open_broker_paper_session
from framework.configuration import load_config, normalize_config
from framework.contracts import OrderIntent
from platforms.opend import (ROOT, SDK_VERSION, OpenDError, BoundedConnection, call,
    configuration_check, connection, load_sdk, select_account, settings, trade_context_class)
from platforms.brokers.futu_paper import FutuPaperConnector
from platforms.brokers.moomoo_paper import MoomooPaperConnector
from platforms.brokers.opend_paper import OpenDPaperBroker, _account
from platforms.data.opend_market import collect

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
SDK = NS(TrdEnv=NS(SIMULATE='SIMULATE', REAL='REAL'), TrdMarket=NS(US='US', NONE='N/A'),
         Currency=NS(USD='USD'), PositionSide=NS(LONG='LONG'), OrderType=NS(NORMAL='NORMAL'),
         TimeInForce=NS(DAY='DAY'), Session=NS(RTH='RTH'), ModifyOrderOp=NS(CANCEL='CANCEL'),
         SimAccType=NS(STOCK='STOCK'), TrdAccStatus=NS(ACTIVE='ACTIVE'),
         KLType=NS(K_DAY='K_DAY'), AuType=NS(QFQ='QFQ'))


class Table:
    def __init__(self, rows):
        self.rows = rows
    def to_dict(self, orient):
        return [dict(row) for row in self.rows]
    def to_json(self, orient):
        return json.dumps(self.rows)


def account_row(**overrides):
    return dict(dict(acc_id=123, trd_env='SIMULATE', sim_acc_type='STOCK', acc_status='ACTIVE',
                     trdmarket_auth=['US'], security_firm='N/A'), **overrides)


class RegistrationTests(unittest.TestCase):
    def test_independent_profiles_preserve_strategy_framework_and_qc_export(self):
        from tools.build_quantconnect import build
        base = normalize_config(load_config(ROOT / 'portfolio_config.json'))
        configs = [normalize_config(load_config(ROOT / (p + '_paper_config.json'))) for p in ('futu', 'moomoo')]
        for cfg in configs:
            self.assertEqual(cfg['strategy'], base['strategy'])
            self.assertEqual(build(cfg), build(base))
            self.assertEqual(_build_platform(cfg['broker'], 'broker').mode, 'broker_paper')
        self.assertNotEqual(configs[0]['broker']['state_dir'], configs[1]['broker']['state_dir'])
        self.assertNotEqual(settings('futu').port, settings('moomoo').port)

    def test_offline_diagnostics_never_import_sdk_or_connect(self):
        with tempfile.TemporaryDirectory() as folder, patch('platforms.opend.import_module') as imported, patch('platforms.opend.socket.create_connection') as socket:
            for provider in ('futu', 'moomoo'):
                self.assertFalse(inspect_broker(provider + '_paper', folder)['network_requested'])
                self.assertFalse(inspect_market_data(provider + '_market', folder)['network_requested'])
            imported.assert_not_called()
            socket.assert_not_called()

    def test_provider_selects_its_own_default_market_config(self):
        with patch('assembly._build_platform') as build:
            for p in ('tiger', 'futu', 'moomoo'):
                inspect_market_data(p + '_market')
                self.assertEqual(build.call_args.kwargs['config_dir'], ROOT / ('secrets/tiger_paper' if p == 'tiger' else 'secrets/' + p))

    def test_unsafe_and_ambiguous_connection_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'connection.json'
            for value in ({'host': '0.0.0.0'}, {'port': True}, {'port': 0}, {'account_id': 123},
                          {'account_id': 'REAL'}, {'environment': 'REAL'}, {'login_user_id': '-1'}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(OpenDError):
                    settings('futu', folder)
            path.write_text(json.dumps({'account_id': '123', 'port': 11113}))
            self.assertEqual(settings('futu', folder).account_id, '123')

    def test_account_selection_excludes_real_derivatives_disabled_and_wrong_region(self):
        cfg = settings('futu')
        self.assertEqual(select_account([account_row(trd_env='REAL', acc_id=9), account_row()], cfg, SDK)[0], '123')
        for row in (account_row(trd_env='REAL'), account_row(sim_acc_type='STOCK_AND_OPTION'),
                    account_row(acc_status='DISABLED'), account_row(trdmarket_auth=['HK']),
                    account_row(security_firm='FUTUAU')):
            with self.subTest(row=row), self.assertRaises(OpenDError):
                select_account([row], cfg, SDK)
        with self.assertRaises(OpenDError):
            select_account([account_row(), account_row(acc_id=456)], cfg, SDK)
        selected = replace(cfg, account_id='456')
        self.assertEqual(select_account([account_row(), account_row(acc_id=456)], selected, SDK)[0], '456')
        with self.assertRaises(OpenDError):
            select_account([account_row(trd_env='REAL', acc_id=456), account_row()], selected, SDK)

    def test_different_state_directory_fails_before_network(self):
        cfg = normalize_config(load_config(ROOT / 'futu_paper_config.json'))
        with tempfile.TemporaryDirectory() as folder, patch('platforms.brokers.opend_paper.connection') as connect:
            with self.assertRaisesRegex(ValueError, 'state directory'):
                with open_broker_paper_session(cfg, None, Path(folder) / 'other', None):
                    self.fail('opened foreign ledger')
            connect.assert_not_called()

    def test_market_diagnostics_cannot_be_used_as_a_trading_snapshot(self):
        for p in ('futu', 'moomoo'):
            with self.assertRaisesRegex(ValueError, 'market_reader'):
                _build_platform({'type': p + '_market'}, 'snapshot_data')
            with self.assertRaises(OpenDError):
                inspect_market_data(p + '_market', claim_device=True)

    def test_initial_login_failure_does_not_leave_an_empty_poisoned_state_directory(self):
        cfg = normalize_config(load_config(ROOT / 'futu_paper_config.json'))
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state'
            cfg['broker']['state_dir'] = str(state)
            with patch('platforms.brokers.opend_paper.connection', side_effect=OpenDError('not logged in')):
                with self.assertRaises(OpenDError):
                    with open_broker_paper_session(cfg, None, None, None):
                        self.fail('opened unauthenticated account')
            self.assertFalse(state.exists())

    def test_failed_initialization_never_removes_a_partial_ledger(self):
        from assembly import _open_simulation_session
        connector = Mock(state_files=('partial.sqlite',))
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state'
            def fail(*args):
                (state / 'partial.sqlite').write_text('evidence')
                raise OpenDError('failed after write')
            connector.initialize.side_effect = fail
            with self.assertRaises(OpenDError):
                with _open_simulation_session(connector, {'initial_cash': 1}, None, state, None):
                    self.fail('opened partial ledger')
            self.assertEqual((state / 'partial.sqlite').read_text(), 'evidence')


class BrokerCases:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.profile = {'provider': self.provider, 'user_id': '111', 'account_id': '123', 'reported_firm': 'N/A'}
        self.client = Mock()
        self.client._paper_account, self.client._allow_orders = '123', True
        self.client.accinfo_query.return_value = (0, Table([{'us_cash': 10000, 'usd_net_cash_power': 9000,
                                                          'cash': 9999999, 'power': 9999999}]))
        self.client.position_list_query.return_value = (0, Table([]))
        self.remote, self.archive = {}, {}
        self.client.order_list_query.side_effect = lambda **kw: (0, Table([
            r for k, r in self.remote.items() if not kw.get('order_id') or kw['order_id'] == k]))
        self.client.history_order_list_query.side_effect = lambda **kw: (0, Table(list(self.archive.values())))
        self.client.place_order.side_effect = self.place
        self.client.modify_order.side_effect = self.cancel
        connector_cls = FutuPaperConnector if self.provider == 'futu' else MoomooPaperConnector
        self.connector = connector_cls(config_dir=str(self.folder), state_dir=str(self.folder))
        @contextmanager
        def fake_connection(*args, **kwargs):
            yield SDK, self.client, self.profile
        with patch('platforms.brokers.opend_paper.connection', fake_connection):
            self.connector.initialize(self.folder, 99999999)
        self.path = self.folder / self.connector.state_files[0]
        self.broker = self.make_broker()
        self.intent = OrderIntent('test-1', 'AAPL', 'BUY', D(10), D(100))

    def make_broker(self, **kwargs):
        broker = OpenDPaperBroker(SDK, self.client, self.profile, self.path, clock=lambda: NOW, **kwargs)
        self.addCleanup(broker.close)
        return broker

    def place(self, **kw):
        oid = str(len(self.remote) + 1)
        row = dict(kw, order_id=oid, order_status='SUBMITTED', dealt_qty=0, dealt_avg_price=0, currency='USD')
        self.remote[oid] = row
        return 0, Table([{'order_id': oid}])  # SDK acknowledgements can contain only the identifier.

    def cancel(self, **kw):
        self.remote[kw['order_id']]['order_status'] = 'CANCELLING_ALL'
        return 0, Table([{'order_id': kw['order_id']}])

    def test_cash_is_available_usd_and_every_read_is_simulation_scoped(self):
        self.assertEqual(self.broker.account().cash, D(9000))
        for api in (self.client.accinfo_query, self.client.position_list_query):
            self.assertEqual(api.call_args.kwargs['trd_env'], 'SIMULATE')
            self.assertEqual(api.call_args.kwargs['acc_id'], 123)
            self.assertTrue(api.call_args.kwargs['refresh_cache'])
        self.client.accinfo_query.return_value = (0, Table([{'cash': 99999, 'power': 99999}]))
        with self.assertRaises(OpenDError):
            self.broker.account()

    def test_repeated_submission_and_restart_do_not_send_twice(self):
        self.assertEqual(self.broker.submit(self.intent).status, 'accepted')
        self.assertEqual(self.broker.submit(self.intent).broker_id, '1')
        self.assertEqual(self.make_broker().submit(self.intent).broker_id, '1')
        self.client.place_order.assert_called_once()
        kw = self.client.place_order.call_args.kwargs
        self.assertEqual((kw['trd_env'], kw['acc_id'], kw['code'], kw['session'], kw['time_in_force']),
                         ('SIMULATE', 123, 'US.AAPL', 'RTH', 'DAY'))
        self.assertFalse(kw['fill_outside_rth'])
        self.assertLessEqual(len(kw['remark'].encode()), 64)

    def test_lost_acknowledgement_recovers_by_tag_without_resubmission(self):
        def lost(**kw):
            self.place(**kw)
            raise TimeoutError('PRIVATE SDK MESSAGE')
        self.client.place_order.side_effect = lost
        self.assertEqual(self.broker.submit(self.intent).status, 'unknown')
        self.assertEqual(self.make_broker().submit(self.intent).status, 'accepted')
        self.client.place_order.assert_called_once()

    def test_timeout_with_no_visible_order_stays_unresolved_across_restart(self):
        self.client.place_order.side_effect = TimeoutError('PRIVATE SDK MESSAGE')
        self.assertEqual(self.broker.submit(self.intent).status, 'unknown')
        restart = self.make_broker()
        self.assertEqual(restart.submit(self.intent).status, 'unknown')
        self.assertTrue(restart.open_orders())
        with self.assertRaises(OpenDError):
            restart.submit(replace(self.intent, client_id='test-2'))
        self.client.place_order.assert_called_once()

    def test_partial_fill_cancel_ack_and_final_cancellation(self):
        self.broker.submit(self.intent)
        self.remote['1'].update(order_status='FILLED_PART', dealt_qty=3, dealt_avg_price=99)
        self.assertEqual(self.broker.query('test-1').filled_quantity, D(3))
        state = self.broker.cancel('1')
        self.assertEqual((state.status, state.filled_quantity), ('pending', D(3)))
        self.assertTrue(self.broker.open_orders())
        self.remote['1']['order_status'] = 'CANCELLED_PART'
        self.assertEqual(self.broker.query('test-1').status, 'cancelled')
        self.assertFalse(self.broker.open_orders())
        kw = self.client.modify_order.call_args.kwargs
        self.assertEqual((kw['modify_order_op'], kw['trd_env'], kw['acc_id']), ('CANCEL', 'SIMULATE', 123))

    def test_prior_day_order_is_recovered_from_history(self):
        self.broker.submit(self.intent)
        self.archive['1'] = self.remote.pop('1')
        self.archive['1'].update(order_status='FILLED_ALL', dealt_qty=10, dealt_avg_price=99)
        self.assertEqual(self.broker.query('test-1').status, 'filled')
        self.assertEqual(self.client.history_order_list_query.call_args.kwargs['start'], '2026-10-01 00:00:00')

    def test_foreign_orders_and_missing_orders_block_execution(self):
        self.remote['555'] = {'order_id': '555', 'order_status': 'UNKNOWN_STATUS'}
        with self.assertRaises(OpenDError):
            self.broker.submit(self.intent)
        self.client.place_order.assert_not_called()
        with self.assertRaises(OpenDError):
            self.broker.cancel('555')
        self.client.modify_order.assert_not_called()

    def test_reused_client_id_and_altered_remote_identity_are_rejected(self):
        self.broker.submit(self.intent)
        with self.assertRaises(OpenDError):
            self.broker.submit(replace(self.intent, quantity=D(20)))
        self.remote['1']['code'] = 'US.MSFT'
        with self.assertRaises(OpenDError):
            self.broker.query('test-1')
        self.client.place_order.assert_called_once()

    def test_fill_cannot_regress_exceed_order_or_violate_limit(self):
        self.broker.submit(self.intent)
        row = self.remote['1']
        row.update(order_status='FILLED_PART', dealt_qty=3, dealt_avg_price=99)
        self.broker.query('test-1')
        for fill, price in ((2, 99), (11, 99), (4, 101)):
            row.update(dealt_qty=fill, dealt_avg_price=price)
            with self.subTest(fill=fill, price=price), self.assertRaises(OpenDError):
                self.broker.query('test-1')

    def test_profiles_and_account_bindings_cannot_share_a_ledger(self):
        for key, value in (('provider', 'futu' if self.provider == 'moomoo' else 'moomoo'),
                           ('user_id', '222'), ('account_id', '456')):
            changed = dict(self.profile, **{key: value})
            self.client._paper_account = changed['account_id']
            with self.subTest(key=key), self.assertRaisesRegex(OpenDError, 'mismatched'):
                OpenDPaperBroker(SDK, self.client, changed, self.path)

    def test_short_fractional_synthetic_and_unaffordable_orders_never_transmit(self):
        for kw in ({'quantity': D('0.1')}, {'symbol': 'DEMOA'}, {'side': 'SHORT'},
                   {'quantity': D(100)}, {'limit_price': D('100.0001')}, {'side': 'SELL'}):
            with self.subTest(kw=kw), self.assertRaises(OpenDError):
                self.broker.submit(replace(self.intent, **kw))
        self.client.place_order.assert_not_called()

    def test_stable_asset_mapping_and_available_sell_quantity(self):
        self.client.position_list_query.return_value = (0, Table([dict(code='US.AAPL', position_side='LONG',
            currency='USD', acc_id=123, qty=10, can_sell_qty=2)]))
        mapped = self.make_broker(symbol_map={'stable-aapl': 'AAPL'})
        self.assertEqual(mapped.account().positions, (('stable-aapl', D(10)),))
        with self.assertRaises(OpenDError):
            mapped.submit(replace(self.intent, symbol='stable-aapl', side='SELL', quantity=D(3)))
        self.assertEqual(mapped.submit(replace(self.intent, symbol='stable-aapl', side='SELL', quantity=D(2))).status, 'accepted')

    def test_sdk_error_messages_do_not_escape(self):
        self.client.accinfo_query.return_value = (-1, 'secret account 123 token PRIVATE')
        with self.assertRaises(OpenDError) as caught:
            self.broker.account()
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertNotIn('123', str(caught.exception))


class FutuBrokerTests(BrokerCases, unittest.TestCase):
    provider = 'futu'


class MoomooBrokerTests(BrokerCases, unittest.TestCase):
    provider = 'moomoo'


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get_global_state.return_value = (0, {'market_us': 'MORNING', 'user_id': 'PRIVATE'})
        self.client.get_history_kl_quota.return_value = (0, (0, 100, []))
        self.client.get_market_snapshot.return_value = (0, Table([dict(code='US.AAPL', update_time='2026-10-01 11:00:00',
            bid_price=99, ask_price=100, suspension=False, internal_account='PRIVATE')]))
        self.bars = [dict(code='US.AAPL', time_key=(NOW - timedelta(days=i)).strftime('%Y-%m-%d 00:00:00'),
                          close=100, volume=1000) for i in range(1, 254)]
        self.client.request_history_kline.return_value = (0, Table(self.bars), None)
        self.client.get_financials_statements.return_value = (0, dict(
            report_list=[{'date_time_str': '2026-06-30', 'item_list': []}], structure_list=[], next_key=-1))

    def test_available_data_is_not_mislabeled_as_full_strategy_ready(self):
        result = collect(SDK, self.client, 'futu', 'AAPL', NOW)
        self.assertEqual(result['completed_daily_bars'], 253)
        self.assertTrue(result['recent_bid_ask'])
        self.assertFalse(result['strategy_ready'])
        self.assertFalse(result['universe_complete'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        kw = self.client.request_history_kline.call_args.kwargs
        self.assertEqual(kw['end'], '2026-09-30')
        self.assertEqual(kw['autype'], 'QFQ')
        self.assertEqual(self.client.get_financials_statements.call_args.kwargs['statement_type'], 1)

    def test_missing_permissions_do_not_fabricate_zero_profit_or_quotes(self):
        self.client.get_market_snapshot.return_value = (-1, 'PRIVATE permission denied')
        self.client.get_financials_statements.return_value = (-1, 'PRIVATE account details')
        result = collect(SDK, self.client, 'moomoo', 'AAPL', NOW)
        self.assertFalse(result['recent_bid_ask'])
        self.assertEqual(result['checks']['financial_statements']['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('data', result['checks']['financial_statements'])

    def test_truncated_duplicate_or_current_day_history_fails_validation(self):
        for rows, token in ((self.bars, b'next'), (self.bars + self.bars[:1], None),
                            ([dict(code='US.AAPL', time_key='2026-10-01 00:00:00', close=100)], None)):
            self.client.request_history_kline.return_value = (0, Table(rows), token)
            result = collect(SDK, self.client, 'futu', 'AAPL', NOW)
            self.assertEqual(result['completed_daily_bars'], 0)
            self.assertEqual(result['checks']['daily_history']['status'], 'failed')


@unittest.skipUnless(all(importlib.util.find_spec(p) for p in ('futu', 'moomoo')), 'Install both optional SDK requirements')
class RealSDKContracts(unittest.TestCase):
    def make_context(self, provider):
        sdk = load_sdk(provider)
        base = importlib.import_module(provider + '.common.open_context_base').OpenContextBase
        cls = trade_context_class(sdk)
        cls.close = lambda self, *a, **kw: None
        with patch.object(base, '__init__', return_value=None):
            ctx = cls(filter_trdmarket=sdk.TrdMarket.US, security_firm=getattr(sdk.SecurityFirm,
                'FUTUSECURITIES' if provider == 'futu' else 'FUTUAU'))
        ctx._paper_account, ctx._allow_orders = '123', True
        ctx.get_sync_conn_id = lambda: 0
        ctx._check_acc_id_and_acc_index = lambda env, acc, index: (0, '', acc, ['US'])
        ctx._check_acc_id = lambda env, acc: (0, '', acc)
        return sdk, base, ctx

    def test_actual_sdk_public_order_methods_pass_guards_and_pack_signatures(self):
        for provider in ('futu', 'moomoo'):
            with self.subTest(provider=provider):
                sdk, base, ctx = self.make_context(provider)
                seen = []
                def factory(this, pack, unpack):
                    def process(**kwargs):
                        inspect.signature(pack).bind(**kwargs)
                        seen.append((pack.__qualname__, kwargs))
                        if pack.__qualname__.startswith('PlaceOrder.'):
                            return 0, '', '777'
                        return 0, '', []
                    return process
                with patch.object(base, '_get_sync_query_processor', factory):
                    reply = ctx.place_order(price=100, qty=1, code='US.AAPL', trd_side=sdk.TrdSide.BUY,
                        trd_env=sdk.TrdEnv.SIMULATE, acc_id=123, session=sdk.Session.RTH,
                        time_in_force=sdk.TimeInForce.DAY, fill_outside_rth=False, remark='offline-test')
                    self.assertEqual(reply[0], 0)
                    self.assertEqual(reply[1].iloc[0]['order_id'], '777')
                    self.assertEqual(ctx.modify_order(sdk.ModifyOrderOp.CANCEL, '777', 0, 0,
                        trd_env=sdk.TrdEnv.SIMULATE, acc_id=123)[0], 0)
                    for method in ('accinfo_query', 'position_list_query', 'order_list_query', 'history_order_list_query'):
                        self.assertEqual(getattr(ctx, method)(trd_env=sdk.TrdEnv.SIMULATE, acc_id=123)[0], 0)
                self.assertTrue(seen)
                self.assertTrue(all(kw['trd_env'] == 'SIMULATE' and kw['acc_id'] == 123 for _, kw in seen))

    def test_real_other_account_read_only_and_unsupported_writes_never_reach_transport(self):
        for provider in ('futu', 'moomoo'):
            with self.subTest(provider=provider):
                sdk, base, ctx = self.make_context(provider)
                transport = Mock(return_value=(0, '', []))
                kwargs = dict(price=100, qty=1, code='US.AAPL', trd_side='BUY',
                              trd_env='SIMULATE', acc_id=123, session='RTH')
                with patch.object(base, '_get_sync_query_processor', return_value=transport):
                    for change in ({'trd_env': 'REAL'}, {'acc_id': 456}, {'acc_id': 0},
                                   {'session': 'ALL'}, {'fill_outside_rth': True}, {'order_type': 'MARKET'}):
                        with self.assertRaises(OpenDError):
                            ctx.place_order(**dict(kwargs, **change))
                    ctx._allow_orders = False
                    with self.assertRaises(OpenDError):
                        ctx.place_order(**kwargs)
                    ctx._allow_orders = True
                    with self.assertRaises(OpenDError):
                        ctx.modify_order(sdk.ModifyOrderOp.NORMAL, '777', 1, 99, trd_env='SIMULATE', acc_id=123)
                    with self.assertRaises(OpenDError):
                        ctx.unlock_trade(password='TEST-ONLY')
                transport.assert_not_called()

    def test_connection_cleanup_and_login_mismatch_without_real_network(self):
        for provider in ('futu', 'moomoo'):
            sdk = load_sdk(provider)
            client = Mock()
            client.get_login_user_id.return_value = 111
            client.get_acc_list.return_value = (0, Table([account_row()]))
            cls = Mock(return_value=client)
            with patch('platforms.opend.socket.create_connection') as socket, patch('platforms.opend.trade_context_class', return_value=cls):
                cfg = replace(settings(provider), login_user_id='222')
                with self.assertRaisesRegex(OpenDError, 'different platform user'):
                    with connection(cfg, trade=True):
                        self.fail('mismatched login accepted')
                client.close.assert_called_once()
                client.reset_mock()
                with connection(settings(provider), trade=True) as (_, _, profile):
                    self.assertEqual(profile['account_id'], '123')
                    self.assertFalse(client._allow_orders)
                client.close.assert_called_once()

    def test_reconnect_hook_does_not_unlock_or_subscribe_real_accounts(self):
        for provider in ('futu', 'moomoo'):
            _, _, ctx = self.make_context(provider)
            ctx.unlock_trade = Mock()
            ctx.get_acc_list = Mock()
            ctx._ctx_unlock = ('TEST-ONLY', None)
            self.assertEqual(ctx.on_api_socket_reconnected(), (0, ''))
            ctx.unlock_trade.assert_not_called()
            ctx.get_acc_list.assert_not_called()

    def test_actual_sdk_account_index_fallback_is_disabled(self):
        for provider in ('futu', 'moomoo'):
            sdk, _, ctx = self.make_context(provider)
            del ctx._check_acc_id_and_acc_index  # Exercise the real adapter override.
            for account in (0, 456):
                with self.assertRaises(OpenDError):
                    ctx._check_acc_id_and_acc_index(sdk.TrdEnv.SIMULATE, account, 0)

    def test_constructor_connection_retry_is_bounded(self):
        class Parent:
            def _init_connect_sync(self):
                return self._auto_reconnect, self._sync_query_connect_timeout
        class Probe(BoundedConnection, Parent):
            pass
        self.assertEqual(Probe()._init_connect_sync(), (False, 5))
