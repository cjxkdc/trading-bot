"""Current data collection: fiscal periods, coverage, caching and quote-only boundary."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.assembly import build_snapshot_collector, platform_catalog
from platforms.data.futu_technology import (CachedQuoteClient, DataIssue, FutuTechnologyData,
    aligned_history, collection_lock, operating_income_ttm)
from platforms.opend import OpenDError

NOW = datetime(2026, 10, 2, 6, tzinfo=timezone.utc)


def reports():
    periods = [(2025, 'Q4', '2025-09-26', 32), (2026, 'Q1', '2025-12-26', 50),
               (2026, 'Q2', '2026-03-27', 35), (2026, 'Q3', '2026-06-26', 36),
               (2025, 'ANNUAL', '2025-09-26', 133)]
    return {'report_list': [{'fiscal_year': year, 'financial_type': kind, 'date_time_str': end,
        'currency_code': 'USD', 'item_list': [{'field_id': 8017, 'display_name': '营业利润', 'data': value}]}
        for year, kind, end, value in periods]}


def sessions():
    return [date(2025, 9, 1) + timedelta(days=i) for i in range(396)
            if (date(2025, 9, 1) + timedelta(days=i)).weekday() < 5]


def history(code='US.AAPL'):
    return [{'code': code, 'time_key': d.isoformat() + ' 00:00:00',
             'close': 100 + i, 'turnover': 20_000_000} for i, d in enumerate(sessions())]


class FinancialTests(unittest.TestCase):
    def test_fiscal_rollover_single_quarters_not_annual_double_count(self):
        result = operating_income_ttm(reports(), NOW)
        self.assertEqual(result['value'], 153)
        self.assertEqual(result['quarter_ends'][-1], '2026-06-26')
        self.assertEqual(result['observed_at'], NOW.isoformat())
        self.assertFalse(result['publication_time_verified'])

    def test_missing_quarter_is_not_zero_filled_or_replaced_with_annual(self):
        raw = reports()
        raw['report_list'].pop(0)
        with self.assertRaisesRegex(DataIssue, 'four_consecutive'):
            operating_income_ttm(raw, NOW)

    def test_newer_annual_missing_q4_rejected(self):
        raw = reports()
        raw['report_list'][-1]['date_time_str'] = '2026-09-26'
        with self.assertRaisesRegex(DataIssue, 'latest_financial_quarter'):
            operating_income_ttm(raw, NOW)

    def test_duplicate_mixed_currency_future_and_wrong_income_field_rejected(self):
        variants = []
        raw = reports(); raw['report_list'].append(deepcopy(raw['report_list'][0])); variants.append(raw)
        raw = reports(); raw['report_list'][0]['currency_code'] = 'HKD'; variants.append(raw)
        raw = reports(); raw['report_list'][3]['date_time_str'] = '2026-12-26'; variants.append(raw)
        raw = reports(); raw['report_list'][0]['item_list'][0]['display_name'] = '持续经营利润'; variants.append(raw)
        raw = reports(); raw['report_list'][0]['item_list'][0]['data'] = float('nan'); variants.append(raw)
        for raw in variants:
            with self.subTest(raw=raw):
                with self.assertRaises(DataIssue):
                    operating_income_ttm(raw, NOW)

    def test_stale_reports_rejected(self):
        with self.assertRaisesRegex(DataIssue, 'stale'):
            operating_income_ttm(reports(), NOW + timedelta(days=300))


class HistoryTests(unittest.TestCase):
    def test_exact_completed_sessions_and_twenty_turnovers(self):
        prices, volumes = aligned_history(history(), 'US.AAPL', sessions(), 253)
        self.assertEqual(len(prices), 253)
        self.assertEqual(len(volumes), 20)
        self.assertEqual(prices[-1], history()[-1]['close'])

    def test_gaps_future_wrong_symbol_duplicate_and_nonpositive_rejected(self):
        variants = [history()[:-1], history() + [history()[-1]], history('US.MSFT')]
        raw = history(); raw[-1]['time_key'] = '2026-10-02 00:00:00'; variants.append(raw)
        raw = history(); raw[-1]['turnover'] = 0; variants.append(raw)
        raw = history(); raw[-1]['close'] = float('inf'); variants.append(raw)
        for raw in variants:
            with self.subTest(length=len(raw)):
                with self.assertRaises(DataIssue):
                    aligned_history(raw, 'US.AAPL', sessions(), 253)


class CacheTests(unittest.TestCase):
    def test_duplicate_collector_rejected_before_any_gateway_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = FutuTechnologyData('unused', 'unused', folder)
            with collection_lock(folder), patch('platforms.data.futu_technology.connection') as connect:
                with self.assertRaisesRegex(DataIssue, '采集任务正在运行'):
                    collector.collect(Path(folder) / 'other-run')
                connect.assert_not_called()
            with collection_lock(folder):
                pass

    def test_exact_request_cache_day_rollover_and_original_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            current = [NOW]
            sdk = Mock()
            sdk.get_plate_stock.return_value = (0, [{'code': 'US.AAPL'}])
            client = CachedQuoteClient(sdk, folder, clock=lambda: current[0], sleep=lambda _: None)
            first = client.request('get_plate_stock', plate_code='US.LIST2075')
            current[0] += timedelta(hours=1)
            cached = client.request('get_plate_stock', plate_code='US.LIST2075')
            self.assertEqual(first, cached)
            self.assertEqual(sdk.get_plate_stock.call_count, 1)
            current[0] += timedelta(days=1)
            self.assertNotEqual(first['observed_at'], client.request('get_plate_stock', plate_code='US.LIST2075')['observed_at'])
            self.assertEqual(sdk.get_plate_stock.call_count, 2)

    def test_failure_not_cached_and_orders_not_in_allowlist(self):
        with tempfile.TemporaryDirectory() as folder:
            sdk = Mock()
            sdk.get_plate_stock.return_value = (-1, 'private SDK failure')
            client = CachedQuoteClient(sdk, folder, clock=lambda: NOW, sleep=lambda _: None)
            with self.assertRaises(OpenDError):
                client.request('get_plate_stock', plate_code='US.LIST2075')
            self.assertFalse(list(Path(folder).rglob('*.json')))
            with self.assertRaises(DataIssue):
                client.request('place_order', code='US.AAPL')
            sdk.place_order.assert_not_called()

    def test_rate_limit_and_uncached_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            sleep = Mock()
            sdk = Mock()
            sdk.get_market_snapshot.return_value = (0, [])
            client = CachedQuoteClient(sdk, folder, clock=lambda: NOW, sleep=sleep, monotonic=lambda: 10)
            for _ in range(2):
                client.request('get_market_snapshot', code_list=['US.AAPL'], use_cache=False)
            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], .55)
            self.assertFalse(list(Path(folder).rglob('*.json')))


class QuoteApi:
    def __init__(self):
        self.hits = 0
        self.calls = []
        self.remaining = 1000
        self.missing_plate = self.bad_history = self.missing_metadata = False

    def request(self, method, **kwargs):
        self.calls.append(method)
        if method == 'request_trading_days':
            value = [{'time': d.isoformat()} for d in sessions()]
        elif method == 'get_plate_list':
            value = [] if self.missing_plate else [{'code': 'US.LIST2075', 'plate_name': '消费电子产品'}]
        elif method == 'get_plate_stock':
            value = [{'code': 'US.AAPL', 'stock_name': 'Apple', 'stock_type': 'STOCK'},
                     {'code': 'US.NEW', 'stock_name': 'New IPO', 'stock_type': 'STOCK'},
                     {'code': 'US.WARRANT', 'stock_name': 'Warrant', 'stock_type': 'WARRANT'}]
        elif method == 'get_stock_basicinfo':
            value = [] if self.missing_metadata else [
                {'code': code, 'stock_id': i + 1, 'stock_type': 'STOCK', 'exchange_type': 'US_NASDAQ',
                 'delisting': False, 'listing_date': listing}
                for i, (code, listing) in enumerate([('US.AAPL', '1980-12-12'), ('US.NEW', '2026-09-01')])]
        elif method == 'get_history_kl_quota':
            value = [0, self.remaining, []]
        elif method == 'request_history_kline':
            value = [history()[:-1] if self.bad_history else history(), None]
        elif method == 'get_financials_statements':
            value = reports()
        elif method == 'get_market_snapshot':
            value = [{'code': 'US.AAPL', 'last_price': 300}]
        else:
            raise AssertionError('Non-quote operation requested: ' + method)
        return {'data': value, 'observed_at': NOW.isoformat()}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.api = QuoteApi()
        self.sdk = SimpleNamespace(TradeDateMarket=SimpleNamespace(US='US'), Market=SimpleNamespace(US='US'),
            Plate=SimpleNamespace(INDUSTRY='INDUSTRY'), SecurityType=SimpleNamespace(STOCK='STOCK'),
            KLType=SimpleNamespace(K_DAY='K_DAY'), AuType=SimpleNamespace(QFQ='QFQ'), Session=SimpleNamespace(RTH='RTH'))
        self.cfg = {'universe_id': 'test', 'max_symbols': 1000, 'industries': {'US.LIST2075': '消费电子产品'}}
        self.collector = FutuTechnologyData('unused', 'unused', 'unused')

    def collect(self):
        with patch('platforms.data.futu_technology.utcnow', return_value=NOW):
            return self.collector._collect(self.api, self.sdk, self.cfg, 253, lambda _: None)

    def test_coverage_exclusions_and_real_observed_time_preserved(self):
        result = self.collect()
        self.assertEqual(result['candidate_count'], 2)
        self.assertEqual(len(result['stocks']), 1)
        self.assertEqual(result['excluded'][0]['code'], 'US.NEW')
        self.assertEqual(result['stocks'][0]['observed_at'], NOW.isoformat())
        self.assertEqual(result['stocks'][0]['price_as_of'], '2026-10-01')
        self.assertEqual(result['issues'], [])

    def test_incomplete_history_is_visible_not_fabricated(self):
        self.api.bad_history = True
        result = self.collect()
        self.assertEqual(result['stocks'], [])
        self.assertFalse(result['collection_complete'])
        self.assertEqual(result['issues'][0]['reason'], 'incomplete_daily_history')
        self.assertNotIn('get_financials_statements', self.api.calls)

    def test_missing_industry_metadata_or_quota_fails_before_histories(self):
        for attribute, value in [('missing_plate', True), ('missing_metadata', True), ('remaining', 0)]:
            with self.subTest(attribute=attribute):
                self.api = QuoteApi()
                setattr(self.api, attribute, value)
                with self.assertRaises(DataIssue):
                    self.collect()
                self.assertNotIn('request_history_kline', self.api.calls)

    def test_plugin_is_separate_from_execution_snapshot_provider(self):
        self.assertEqual(platform_catalog()['futu_technology']['kind'], 'snapshot_collector')
        self.assertIsInstance(build_snapshot_collector({'type': 'futu_technology',
            'config_dir': 'unused', 'source_config': 'unused', 'cache_dir': 'unused'}), FutuTechnologyData)

    def test_collect_uses_quote_context_and_preserves_data_failure_reason(self):
        @contextmanager
        def quote_connection(*args, **kwargs):
            self.assertFalse(kwargs.get('trade', False))
            self.assertFalse(kwargs.get('allow_orders', False))
            try:
                yield self.sdk, Mock(), {}
            except Exception:
                self.fail('Data failures must not become gateway login errors')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = folder / 'config.json'
            config.write_text(json.dumps(dict(self.cfg, schema_version=1)))
            collector = FutuTechnologyData(folder, config, folder / 'cache')
            with patch('platforms.data.futu_technology.connection', side_effect=quote_connection), \
                    patch.object(collector, '_collect', side_effect=DataIssue('quota_missing')):
                with self.assertRaisesRegex(DataIssue, 'quota_missing'):
                    collector.collect(folder / 'out')
            self.assertFalse((folder / 'out/preview-data.json').exists())
            self.assertIn('quota_missing', json.loads((folder / 'out/progress.json').read_text(encoding='utf-8'))['message'])


if __name__ == '__main__':
    unittest.main()
