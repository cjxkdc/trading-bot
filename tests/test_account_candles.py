"""Observed equity candles, calendar boundaries and read-only API contracts."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from app.control_contracts import ControlError
from app.notifications import NotificationSettings
from app.workbench import PortalService
from framework.account_candles import bounds
from framework.performance import PerformanceStore, stamp
from portal.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def observation(equity, at):
    return dict(environment='SIMULATE', account_ref='test-account', currency='USD',
                as_of=at, equity=equity, cash_total=equity, cash_available=equity,
                market_value=0, positions=[])


class CandleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'performance.sqlite'
        self.store = PerformanceStore(self.path)

    def record(self, equity, at, scope='a'):
        return self.store.record(scope, observation(equity, at))

    def test_intrahour_extrema_keep_every_observation_and_chronological_open_close(self):
        # Arrival order is deliberately different from observation order.
        for at, equity in [('14:55',105), ('14:15',90), ('14:00',100), ('14:40',120), ('15:00',110)]:
            self.record(equity, f'2025-01-02T{at}:00Z')
        report = self.store.candles('a', '1h', now=stamp('2025-01-02T15:30:00Z'))
        a,b = report['bars']
        self.assertEqual([a[k] for k in ('open','high','low','close','sample_count')], [100,120,90,105,4])
        self.assertEqual(a['first_observation'], '2025-01-02T14:00:00+00:00')
        self.assertFalse(a['in_progress']); self.assertTrue(b['in_progress'])
        self.assertEqual([b[k] for k in ('open','high','low','close','sample_count')], [110,110,110,110,1])
        # Existing daily accounting metrics remain independent of OHLC.
        self.assertEqual([p['equity'] for p in self.store.report('a')['curve']], [100,110])
        restarted = PerformanceStore(self.path)
        self.assertEqual(restarted.candles('a','1h')['bars'][0]['high'], 120)

    def test_minutes_and_five_minutes_are_different_and_missing_windows_stay_empty(self):
        for at, equity in [('14:00:05',100), ('14:00:50',110), ('14:04:10',90), ('14:10:00',120)]:
            self.record(equity, f'2025-01-02T{at}Z')
        minute = self.store.candles('a','1m')['bars']
        self.assertEqual([b['sample_count'] for b in minute], [2,1,1])
        self.assertEqual(minute[0]['close'], 110)
        five = self.store.candles('a','5m')['bars']
        self.assertEqual([b['sample_count'] for b in five], [3,1])
        self.assertEqual(five[0]['low'], 90)
        self.assertEqual(five[1]['start'], '2025-01-02T14:10:00+00:00')
        # A range means a calendar horizon, not N observed bars across long gaps.
        self.assertEqual(len(self.store.candles('a','1m',2)['bars']),1)
        self.assertEqual(self.store.candles('missing')['bars'], [])

    def test_new_york_day_and_monday_week_not_utc_or_host_calendar(self):
        self.record(100, '2025-01-06T03:59:00Z')  # NY Sunday
        self.record(110, '2025-01-06T05:01:00Z')  # NY Monday
        days = self.store.candles('a','1d')['bars']
        self.assertEqual([b['start'] for b in days], ['2025-01-05T05:00:00+00:00','2025-01-06T05:00:00+00:00'])
        weeks = self.store.candles('a','1w')['bars']
        self.assertEqual([b['start'] for b in weeks], ['2024-12-30T05:00:00+00:00','2025-01-06T05:00:00+00:00'])

    def test_dst_repeated_hours_are_distinct_and_days_have_actual_duration(self):
        self.record(100, '2025-11-02T01:15:00-04:00')
        self.record(110, '2025-11-02T01:15:00-05:00')
        bars = self.store.candles('a','1h')['bars']
        self.assertEqual([b['start'] for b in bars], ['2025-11-02T05:00:00+00:00','2025-11-02T06:00:00+00:00'])
        for at, expected in [('2025-03-09T12:00:00Z',23), ('2025-11-02T12:00:00Z',25)]:
            start,end = bounds(stamp(at),'1d')
            self.assertEqual((end-start).total_seconds()/3600,expected)
        start,end = bounds(stamp('2025-03-09T12:00:00Z'),'1w')
        self.assertEqual((end-start).total_seconds()/3600,167)

    def test_reset_starts_new_series_scope_isolated_and_deposit_affects_raw_equity(self):
        self.record(100,'2025-01-02T14:00:00Z')
        self.record(150,'2025-01-02T14:15:00Z')
        self.record(155,'2025-01-02T14:30:00Z')
        self.record(9000,'2025-01-02T14:05:00Z',scope='b')
        self.store.cash_event('a',dict(as_of='2025-01-02T14:15:00Z',kind='deposit',amount=50,note=''))
        self.assertEqual(self.store.candles('a')['bars'][0]['high'],155)
        self.store.cash_event('a',dict(as_of='2025-01-02T14:15:00Z',kind='reset',amount=0,note=''))
        bar = self.store.candles('a')['bars'][0]
        self.assertEqual([bar[k] for k in ('open','high','low','close','sample_count')],[150,155,150,155,2])
        self.assertEqual(self.store.candles('b')['bars'][0]['open'],9000)
        self.assertTrue(self.store.candles('a')['sampling_only'])
        self.assertNotIn('account_ref',json.dumps(self.store.candles('a')))

    def test_unsupported_period_range_and_naive_clock_fail(self):
        for interval, limit in [('1s',96),("1h'; DROP TABLE observations",96),([],96),('1h',0),('1h',241),('1h',True)]:
            with self.assertRaises(ValueError): self.store.candles('a',interval,limit)
        with self.assertRaises(ValueError): self.store.candles('a',now=datetime(2025,1,2))
        self.record(100,'2025-01-02T14:00:00Z')
        self.assertEqual(len(self.store.candles('a')['bars']),1)


class CandleAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ('configs','strategies','framework'):
            shutil.copytree(ROOT/folder,self.root/folder,ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('brokers','data'):
            shutil.copytree(ROOT/folder/'plugins',self.root/folder/'plugins')
        self.worker = Mock(side_effect=AssertionError('No broker operation permitted'))
        self.service = PortalService(self.root,worker=self.worker)
        self.service.start_scheduler = lambda: None
        self.scope = patch.object(self.service.monitor,'scope',return_value='test-scope')
        self.scope.start(); self.addCleanup(self.scope.stop)
        self.service.monitor.performance.record('test-scope',observation(1000,'2025-01-02T14:00:00Z'))
        self.client = TestClient(create_app(self.root,service=self.service),base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.client.headers['X-Portal-Token'] = self.client.get('/api/session').json()['token']

    def test_candle_api_is_read_only_authenticated_account_bound(self):
        key = self.service.performance()['account_key']
        response = self.client.get('/api/performance/candles',params={'interval':'1h','account_key':key})
        self.assertEqual(response.status_code,200)
        result = response.json()
        self.assertEqual(result['bars'][0]['close'],1000)
        self.assertEqual(result['account_key'],key)
        self.assertNotIn('test-account',response.text)
        for invalid in ('wrong',''):
            self.assertEqual(self.client.get('/api/performance/candles',headers={'X-Portal-Token':invalid}).status_code,403)
        with patch.object(self.service.monitor,'scope',return_value='other-account'):
            self.assertEqual(self.client.get('/api/performance/candles',params={'account_key':key}).status_code,409)
        self.worker.assert_not_called()
        self.assertFalse(self.service.automation.data['enabled'])

    def test_unbound_account_returns_empty_and_invalid_queries_fail(self):
        with patch.object(self.service.monitor,'scope',side_effect=ControlError('请绑定模拟账户。')):
            result = self.client.get('/api/performance/candles')
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()['bars'],[])
        for query in ({'interval':'2h'},{'limit':0},{'limit':999}):
            self.assertEqual(self.client.get('/api/performance/candles',params=query).status_code,409)
        self.assertEqual(self.client.get('/api/performance/candles?limit=oops').status_code,422)
        self.worker.assert_not_called()

    def test_one_minute_is_opt_in_and_persists_without_enabling_trading(self):
        settings = self.service.monitor.settings
        self.assertEqual(settings.config['interval_seconds'],900)
        before = deepcopy(self.service.automation.data)
        settings.save({'interval_seconds':60})
        self.assertEqual(NotificationSettings(self.root).config['interval_seconds'],60)
        for value in (59,0,True,3601):
            with self.assertRaises(ControlError): settings.save({'interval_seconds':value})
        self.assertEqual(NotificationSettings(self.root).config['interval_seconds'],60)
        self.assertEqual(self.service.automation.data,before)
        self.worker.assert_not_called()


if __name__ == '__main__': unittest.main()
