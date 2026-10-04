"""Offline HTTP contract and financial data tests; no credentials or live API."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from data.massive import (MassiveClient, MassiveError, NoRedirect, Settings, TICKERS, INCOME,
                         canonical_url, configuration_status, save_settings, settings)
from data.massive_technology import (MassiveTechnologyData, aligned_prices, latest_income,
                                    source_settings, SCHEMA)
from data.common import DataIssue, collection_lock
from data.snapshot import parse_snapshot

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 14, tzinfo=timezone.utc)
KEY = 'TEST_ONLY_FAKE_KEY_1234567890'


class Clock:
    def __init__(self): self.elapsed = 0
    def now(self): return NOW + timedelta(seconds=self.elapsed)
    def monotonic(self): return self.elapsed
    def sleep(self, seconds): self.elapsed += seconds


class Reply(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(json.dumps(data).encode())
        self.url = url
    def geturl(self): return self.url


class TransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root, self.clock = Path(temp.name), Clock()
        self.opener = Mock()
        self.config = Settings(KEY, 5)
        self.client = MassiveClient(self.config, self.root, opener=self.opener,
                                   clock=self.clock.now, monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    def reply(self, payload):
        self.opener.open.side_effect = lambda req, timeout: Reply(payload, req.full_url)

    def test_header_auth_cache_redaction_and_original_observation(self):
        self.reply({'status': 'OK', 'results': [{'ticker': 'AAPL', 'note': KEY}], 'message': KEY,
                    'next_url': 'https://api.massive.com/v3/reference/tickers?cursor=next&apiKey=' + KEY})
        first = self.client.request(TICKERS)
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer ' + KEY)
        self.assertNotIn(KEY, req.full_url)
        self.clock.sleep(10)
        second = self.client.request(TICKERS)
        self.assertEqual(second, first)
        self.assertEqual(self.opener.open.call_count, 1)
        for path in self.root.rglob('*.json'):
            self.assertNotIn(KEY, path.read_text())
        self.assertNotIn(KEY, repr(self.config))

    def test_success_cache_and_observed_time_do_not_bypass_entitlement_probe(self):
        self.reply({'status': 'OK', 'results': []})
        first = self.client.request(TICKERS)
        self.clock.sleep(86401)
        second = self.client.request(TICKERS)
        self.assertNotEqual(first['observed_at'], second['observed_at'])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_reject_redirects_wrong_hosts_and_endpoint_changes_before_auth(self):
        for url in ('http://api.massive.com/v3/reference/tickers',
                    'https://api.massive.com.evil.test/v3/reference/tickers',
                    'https://evil.test/v3/reference/tickers',
                    'https://api.massive.com:443/v3/reference/tickers',
                    'https://user:password@api.massive.com/v3/reference/tickers',
                    'https://api.massive.com/v3/reference/tickers#x',
                    'https://api.massive.com/v3/reference/tickers/../../orders',
                    'https://api.massive.com/v3/reference/tickers?cursor=x&cursor=y'):
            with self.subTest(url=url), self.assertRaises(MassiveError):
                self.client.request(url)
        self.opener.open.assert_not_called()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test'))
        with self.assertRaises(MassiveError):
            canonical_url('https://api.massive.com' + INCOME, expected_path=TICKERS)

    def test_hostile_next_page_does_not_cache_or_forward_key(self):
        self.reply({'status': 'OK', 'results': [], 'next_url': 'https://evil.test/collect'})
        with self.assertRaises(MassiveError): self.client.pages(TICKERS)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual([p.name for p in self.root.rglob('*.json')], ['last-request.json'])

    def test_pagination_accumulates_all_and_rejects_cycle_and_bounds(self):
        def reply(req, timeout):
            return Reply({'status': 'OK', 'results': [{'ticker': 'B'}]} if 'cursor=' in req.full_url else
                         {'status': 'OK', 'results': [{'ticker': 'A'}], 'next_url':
                          'https://api.massive.com/v3/reference/tickers?cursor=two'}, req.full_url)
        self.opener.open.side_effect = reply
        self.assertEqual([r['ticker'] for r in self.client.pages(TICKERS)['results']], ['A', 'B'])
        with self.assertRaisesRegex(MassiveError, '条数'): self.client.pages(TICKERS, max_rows=1)
        with self.assertRaisesRegex(MassiveError, '分页'): self.client.pages(TICKERS, max_pages=1)
        with patch.object(self.client, 'request', return_value={'observed_at': NOW.isoformat(), 'data': {
            'results': [], 'next_url': 'https://api.massive.com/v3/reference/tickers'}}):
            with self.assertRaisesRegex(MassiveError, '重复'): self.client.pages(TICKERS)

    def test_403_is_sanitized_not_retried_or_cached(self):
        self.opener.open.side_effect = HTTPError('https://api.massive.com/?apiKey=' + KEY, 403, KEY, {}, None)
        with self.assertRaises(MassiveError) as error: self.client.request(INCOME)
        self.assertEqual(error.exception.code, 'entitlement')
        self.assertNotIn(KEY, str(error.exception))
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual([p.name for p in self.root.rglob('*.json')], ['last-request.json'])

    def test_429_bounded_retry_spacing_and_budget(self):
        self.opener.open.side_effect = [HTTPError('x', 429, 'rate', {'Retry-After': '1'}, None),
                                        Reply({'status': 'OK', 'results': []}, 'https://api.massive.com' + TICKERS)]
        self.client.request(TICKERS)
        self.assertGreaterEqual(self.clock.elapsed, 12)
        self.assertEqual(self.client.requests, 2)
        self.client.max_requests = 2
        with self.assertRaises(MassiveError) as error: self.client.request(INCOME)
        self.assertEqual(error.exception.code, 'budget')

    def test_new_worker_keeps_rate_spacing_and_can_cancel_while_waiting(self):
        self.reply({'status': 'OK', 'results': []})
        self.client.request(TICKERS)
        other = MassiveClient(self.config, self.root, opener=self.opener,
                              clock=self.clock.now, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        other.request(INCOME)
        self.assertGreaterEqual(self.clock.elapsed, 12)
        other.cancelled = lambda: self.clock.elapsed > 13
        with self.assertRaises(MassiveError) as error: other.request(TICKERS + '/AAPL')
        self.assertEqual(error.exception.code, 'cancelled')
        self.assertEqual(self.opener.open.call_count, 2)

    def test_daily_rejects_wrong_symbol_basis_or_truncated_response(self):
        for change in ({'ticker': 'MSFT'}, {'adjusted': False}, {'next_url':
                       'https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-31?cursor=x'}):
            payload = {'ticker': 'AAPL', 'adjusted': True, 'results': [], 'status': 'OK'}
            payload.update(change)
            with patch.object(self.client, 'request', return_value={'data': payload}):
                with self.assertRaises(MassiveError): self.client.daily('AAPL', '2026-01-01', '2026-01-31')

    def test_configuration_missing_offline_and_local_precedence(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(configuration_status(self.root)['configured'])
        save_settings(self.root, KEY, 60)
        with patch.dict('os.environ', {'MASSIVE_API_KEY': 'OTHER_ENV_KEY_1234567890'}):
            self.assertEqual(settings(self.root).api_key, KEY)
        self.assertNotIn(KEY, str(configuration_status(self.root)))


def financial(ticker='AAPL', **changes):
    value = {'tickers': [ticker], 'cik': '000123', 'timeframe': 'trailing_twelve_months',
             'period_end': '2026-06-30', 'filing_date': '2026-08-01', 'fiscal_quarter': 2,
             'operating_income': 1000000.0}
    value.update(changes)
    return value


class FinancialTests(unittest.TestCase):
    def test_requires_filing_cutoff_identity_ttm_and_nonstale_finite_income(self):
        day = date(2026, 10, 1)
        result = latest_income([financial()], 'AAPL', '123', day)
        self.assertEqual(result['value'], 1000000)
        for change in ({'timeframe': 'quarterly'}, {'filing_date': '2026-10-02'},
                       {'period_end': '2025-01-01'}, {'tickers': ['MSFT']},
                       {'cik': '321'}, {'operating_income': float('nan')}, {'operating_income': True}):
            with self.subTest(change=change), self.assertRaises(DataIssue):
                latest_income([financial(**change)], 'AAPL', '123', day)
        with self.assertRaises(DataIssue): latest_income([], 'AAPL', '123', day)

    def test_restatements_choose_latest_filing_without_summing_ttm_periods(self):
        records = [financial(), financial(filing_date='2026-09-01', operating_income=-10)]
        self.assertEqual(latest_income(records, 'AAPL', '123', date(2026, 10, 1))['value'], -10)
        records.append(financial(filing_date='2026-09-01', operating_income=20))
        with self.assertRaises(DataIssue): latest_income(records, 'AAPL', '123', date(2026, 10, 1))


def bars(sessions, adjusted):
    return {'observed_at': NOW.isoformat(), 'data': {'ticker': 'AAPL', 'adjusted': adjusted,
        'results': [{'t': int(datetime(d.year, d.month, d.day, 4, tzinfo=timezone.utc).timestamp() * 1000),
                     'c': 50 if adjusted else 100, 'v': 300000} for d in sessions]}}


class PriceTests(unittest.TestCase):
    def test_dollars_use_unadjusted_close_and_volume_not_split_price(self):
        sessions = [date(2026, 9, i) for i in (1, 2, 3)]
        adj, raw = bars(sessions, True), bars(sessions, False)
        prices, _, _, dollars = aligned_prices(adj, raw, sessions, sessions[0], sessions[-1])
        self.assertEqual(prices, [50] * 3)
        self.assertEqual(dollars, [30000000] * 3)
        raw['data']['results'].pop()
        with self.assertRaisesRegex(DataIssue, 'incomplete'): aligned_prices(adj, raw, sessions, sessions[0], sessions[-1])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / 'source.json'
        self.source = json.loads((ROOT / 'configs/massive_technology.json').read_text(encoding='utf-8'))
        self.config.write_text(json.dumps(self.source))
        save_settings(self.root / 'secrets', KEY, 600)
        self.collector = MassiveTechnologyData(self.root / 'secrets', self.config, self.root / 'cache')
        self.days = sorted(date(2026, 10, 1) - timedelta(days=i) for i in range(600)
                           if (date(2026, 10, 1) - timedelta(days=i)).weekday() < 5)[-253:]
        self.api = Mock(requests=8, hits=0)
        self.api.request.side_effect = self.detail
        self.api.pages.side_effect = self.pages
        self.api.daily.side_effect = lambda symbol, start, end, adjusted=True: bars(self.days, adjusted)

    def detail(self, path, **kwargs):
        ticker = path.rsplit('/', 1)[-1]
        return {'observed_at': NOW.isoformat(), 'data': {'results': {
            'ticker': ticker, 'type': 'CS', 'market': 'stocks', 'locale': 'us', 'active': True,
            'currency_name': 'usd', 'primary_exchange': 'XNAS', 'cik': '000123',
            'sic_code': '6021' if ticker == 'BANK' else '7372', 'share_class_figi': 'FIGI' + ticker,
            'list_date': '2000-01-01'}}}

    def pages(self, path, params, **kwargs):
        if path == TICKERS:
            rows = [self.detail(TICKERS + '/' + t)['data']['results'] for t in ['AAPL', 'MSFT', 'BANK']]
        else:
            rows = [financial(params['tickers'])]
        return {'results': rows, 'observed_at': NOW.isoformat()}

    def collect(self):
        with patch('data.massive_technology.MassiveClient', return_value=self.api), \
             patch('data.massive_technology.utcnow', return_value=NOW):
            return self.collector.collect(self.root / 'out')

    def test_end_to_end_current_research_does_not_become_executable_or_pit(self):
        raw = json.loads(self.collect().read_text(encoding='utf-8'))
        self.assertEqual({s['ticker'] for s in raw['stocks']}, {'AAPL', 'MSFT'})
        self.assertEqual(raw['schema'], SCHEMA)
        self.assertFalse(raw['execution_allowed'])
        self.assertFalse(raw['historical_point_in_time'])
        self.assertEqual(raw['quotes'], [])
        self.assertTrue(raw['collection_complete'])
        self.assertEqual(raw['stocks'][0]['price_basis'], 'split_adjusted_only')
        self.assertEqual(raw['stocks'][0]['observed_at'], NOW.isoformat())
        with self.assertRaisesRegex(ValueError, 'Research-only'): parse_snapshot(raw)
        self.assertNotIn(KEY, json.dumps(raw))

    def test_permissions_fail_before_universe_scan_and_preserve_no_success_file(self):
        self.api.pages.side_effect = MassiveError('entitlement', 'Missing financials access')
        with self.assertRaises(MassiveError): self.collect()
        self.assertFalse((self.root / 'out/preview-data.json').exists())
        self.assertTrue((self.root / 'out/failure.json').exists())
        self.assertNotIn(TICKERS, [args.args[0] for args in self.api.pages.call_args_list])

    def test_missing_stock_financials_are_visible_and_never_disable_profit_filter(self):
        def pages(path, params, **kwargs):
            result = self.pages(path, params, **kwargs)
            if params.get('tickers') == 'MSFT': result['results'] = []
            return result
        self.api.pages.side_effect = pages
        raw = json.loads(self.collect().read_text(encoding='utf-8'))
        self.assertFalse(raw['collection_complete'])
        self.assertEqual(raw['candidate_count'], 2)
        self.assertEqual([s['ticker'] for s in raw['stocks']], ['AAPL'])
        self.assertEqual(raw['issues'], [{'ticker': 'MSFT', 'reason': 'no_ttm_operating_income'}])

    def test_duplicate_full_universe_is_not_silently_truncated(self):
        def pages(path, params, **kwargs):
            result = self.pages(path, params, **kwargs)
            if path == TICKERS: result['results'] += result['results'][:1]
            return result
        self.api.pages.side_effect = pages
        with self.assertRaisesRegex(DataIssue, 'duplicate'): self.collect()

    def test_competing_collector_stops_before_network(self):
        with collection_lock(self.root / 'cache'):
            with self.assertRaises(DataIssue): self.collect()
        self.api.request.assert_not_called()

    def test_explicit_subset_is_labeled_and_does_not_fetch_market_members(self):
        self.source.update(universe_mode='explicit_tickers', tickers=['MSFT'])
        self.config.write_text(json.dumps(self.source))
        raw = json.loads(self.collect().read_text(encoding='utf-8'))
        self.assertIn('explicit_tickers', raw['universe_id'])
        self.assertTrue(any('手工' in text for text in raw['limitations']))
        self.assertNotIn(TICKERS, [args.args[0] for args in self.api.pages.call_args_list])
