import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from app.assembly import _build_platform, inspect_market_data
from platforms.data.tiger_market import (READ_METHODS, TigerMarketData, TigerMarketError,
    _client_class, _completed_bars, _connect, _probe, _quote_usable, collect)

HAS_SDK = importlib.util.find_spec('tigeropen') is not None


class MarketTests(unittest.TestCase):
    def test_offline_does_not_connect_and_reader_is_not_a_trading_snapshot_provider(self):
        with tempfile.TemporaryDirectory() as folder, patch(
                'platforms.data.tiger_market._connect') as connect:
            result = inspect_market_data('tiger_market', folder)
            self.assertFalse(result['network_requested'])
            self.assertFalse(result['strategy_ready'])
            connect.assert_not_called()
        for kind in ('broker', 'snapshot_data'):
            with self.assertRaisesRegex(ValueError, 'market_reader'):
                _build_platform({'type': 'tiger_market'}, kind, config_dir='unused')

    def test_explicit_device_claim_requires_a_network_request(self):
        with self.assertRaisesRegex(TigerMarketError, 'requires --connect'):
            TigerMarketData('.').inspect(claim_device=True)
        with self.assertRaises(TigerMarketError):
            TigerMarketData('.').inspect(symbol='AAPL,MSFT')

    def test_missing_data_and_sdk_errors_never_become_fabricated_values(self):
        self.assertEqual(_probe(lambda: None, ('value',))['status'], 'empty')
        error = RuntimeError('PRIVATE KEY signed URL')
        error.msg, error.code = 'Permission denied device SECRET', '4'
        result = _probe(Mock(side_effect=error), ('value',))
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['permission_error'])
        self.assertNotIn('data', result)
        self.assertNotIn('SECRET', json.dumps(result))
        result = _probe(lambda: [{'symbol': 'AAPL', 'private_key': 'SECRET'}], ('symbol',))
        self.assertEqual(result['data'], [{'symbol': 'AAPL'}])

    def test_completed_history_excludes_execution_day_and_rejects_duplicate_sessions(self):
        now = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
        def row(days):
            return {'symbol': 'AAPL', 'close': 100, 'time': (now - timedelta(days=days)).timestamp() * 1000}
        self.assertEqual(_completed_bars({'data': [row(2), row(1), row(0)]}, 'AAPL', now), 2)
        self.assertEqual(_completed_bars({'data': [row(1), row(1)]}, 'AAPL', now), 0)
        self.assertEqual(_completed_bars({'data': [dict(row(1), close=float('nan'))]}, 'AAPL', now), 0)
        self.assertEqual(_completed_bars({'data': [dict(row(1), symbol='MSFT')]}, 'AAPL', now), 0)

    def test_delayed_quotes_cannot_pass_bid_ask_recency_check(self):
        now = datetime.now(timezone.utc)
        row = {'symbol': 'AAPL', 'status': 'NORMAL', 'bid_price': 100, 'ask_price': 101,
               'latest_time': now.timestamp() * 1000}
        self.assertTrue(_quote_usable({'data': [row]}, 'AAPL', now))
        for override in ({'latest_time': (now.timestamp() - 900) * 1000},
                         {'latest_time': (now.timestamp() + 1) * 1000},
                         {'bid_price': 102}, {'status': 'HALTED'}, {'ask_price': float('nan')}):
            self.assertFalse(_quote_usable({'data': [dict(row, **override)]}, 'AAPL', now))
        self.assertFalse(_quote_usable({'data': [{'symbol': 'AAPL', 'close': 100,
                                                'time': now.timestamp() * 1000}]}, 'AAPL', now))

    def test_existing_output_is_never_overwritten_or_used_for_network_retry(self):
        import bot
        with tempfile.TemporaryDirectory() as folder, patch('sys.argv',
                ['bot.py', 'market-status', '--connect', '--out', folder]), patch(
                    'bot.inspect_market_data') as inspect:
            with self.assertRaises(FileExistsError):
                bot.main()
            inspect.assert_not_called()


@unittest.skipUnless(HAS_SDK, 'Install requirements/tiger.txt for SDK contract tests')
class MarketSdkTests(unittest.TestCase):
    def test_sdk_constructor_never_claims_device_access_implicitly(self):
        from tigeropen.tiger_open_client import TigerOpenClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {}, clear=True):
            (Path(folder) / 'tiger_openapi_config.properties').write_text(
                'tiger_id=TEST\naccount=12345678901234567\nlicense=TBAU\nprivate_key_pk8=TEST-ONLY\nenv=PROD\n')
            with patch.object(TigerOpenClient, 'execute', side_effect=AssertionError('Network forbidden')), patch.object(
                    TigerOpenClientConfig, 'query_domains', side_effect=AssertionError('Network forbidden')):
                client = _connect(folder)
                self.assertFalse(client._claim_authorized)

    def test_request_guard_blocks_orders_and_allows_only_one_authorized_claim(self):
        from tigeropen.quote.quote_client import QuoteClient
        client = _client_class().__new__(_client_class())
        with patch.object(QuoteClient, 'execute', return_value='{}') as execute:
            for method in READ_METHODS:
                self.assertEqual(client.execute(NS(_method=method)), '{}')
            for method in ('place_order', 'cancel_order', 'transfer', 'grab_quote_permission'):
                with self.assertRaises(TigerMarketError):
                    client.execute(NS(_method=method))
            client._claim_authorized = True
            client.execute(NS(_method='grab_quote_permission'))
            with self.assertRaises(TigerMarketError):
                client.execute(NS(_method='grab_quote_permission'))
            with self.assertRaises(TigerMarketError):
                client.execute(NS(_method='kline'), url='https://invalid.example')
            self.assertEqual(execute.call_count, len(READ_METHODS) + 1)

    def test_single_symbol_probe_keeps_errors_and_never_claims_full_strategy_readiness(self):
        client = Mock()
        client.get_quote_permission.return_value = [{'name': 'aStockQuoteLv1', 'expire_at': -1}]
        client.get_kline_quota.return_value = [{'method': 'kline', 'remain': 20, 'used': 0}]
        client.get_stock_briefs.side_effect = RuntimeError('SECRET')
        client.get_stock_delay_briefs.return_value = [{'symbol': 'AAPL', 'close': 100, 'time': 1}]
        client.get_bars.return_value = []
        client.get_stock_industry.return_value = [{'industry_level': 'GSECTOR', 'id': '45'}]
        client.get_financial_report.side_effect = RuntimeError('SECRET')
        result = collect(client, 'AAPL')
        self.assertFalse(result['strategy_ready'])
        self.assertFalse(result['universe_complete'])
        self.assertFalse(result['orders_submitted'])
        self.assertFalse(result['recent_two_sided_quote'])
        self.assertEqual(result['checks']['delayed']['status'], 'available')
        self.assertEqual(result['checks']['operating_income_ttm']['status'], 'failed')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('input_schema', result)
        client.grab_quote_permission.assert_not_called()
        for call in client.get_bars.call_args_list:
            self.assertEqual(call.args[0], ['AAPL'])
            self.assertEqual(call.kwargs['limit'], 260)
