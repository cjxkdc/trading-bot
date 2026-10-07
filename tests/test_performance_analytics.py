"""Cash-adjusted range results and observation boundaries used by the account UI."""
from pathlib import Path
import tempfile
import unittest

from framework.performance import PerformanceStore
from framework.performance_analytics import cutoff, position_summary


class AccountRangeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = PerformanceStore(Path(tmp.name) / 'observations.sqlite')

    def record(self, at, equity, positions=None, scope='paper'):
        return self.store.record(scope, dict(environment='SIMULATE', account_ref=scope,
            currency='USD', as_of=at, equity=equity, positions=positions or []))

    def flow(self, at, kind, amount):
        return self.store.cash_event('paper', dict(as_of=at, kind=kind, amount=amount, note=''))

    def report(self):
        return self.store.report('paper')['analytics']

    def test_new_sample_preserves_exact_reviewed_intraday_result(self):
        self.record('2026-10-05T13:30:00Z', 1000)
        reviewed = self.record('2026-10-05T15:00:00Z', 1100)
        self.store.review_cash('paper', reviewed['as_of'])
        self.record('2026-10-05T16:00:00Z', 5600)
        report = self.report()['ALL']
        self.assertIsNone(report['metrics']['profit'])
        self.assertIsNone(report['metrics']['return_rate'])
        self.assertEqual(report['verified']['through'], reviewed['as_of'])
        self.assertEqual(report['verified']['profit'], 100)
        self.assertAlmostEqual(report['verified']['return_rate'], .1)
        self.assertEqual(report['points'][-1]['equity'], 5600)
        self.assertIsNone(report['points'][-1]['profit'])

    def test_deposits_withdrawals_dividends_and_fees_are_not_double_counted(self):
        self.record('2026-09-01T20:00:00Z', 1000)
        self.record('2026-09-02T20:00:00Z', 1600)
        last = self.record('2026-09-03T20:00:00Z', 1335)
        self.flow('2026-09-02T20:00:00Z', 'deposit', 500)
        self.flow('2026-09-03T20:00:00Z', 'withdrawal', 200)
        self.flow('2026-09-03T19:00:00Z', 'dividend', 10)
        self.flow('2026-09-03T19:00:00Z', 'fee', 5)
        self.store.review_cash('paper', last['as_of'])
        metrics = self.report()['ALL']['metrics']
        self.assertEqual(metrics['profit'], 35)
        self.assertEqual(metrics['net_flow'], 300)
        self.assertAlmostEqual(metrics['return_rate'], 1.1 * (1 - 65/1600) - 1)
        self.assertAlmostEqual(metrics['max_drawdown'], 65/1600)

    def test_half_interval_deposit_uses_time_weighted_capital(self):
        self.record('2026-09-01T20:00:00Z', 1000)
        last = self.record('2026-09-02T20:00:00Z', 1650)
        self.flow('2026-09-02T08:00:00Z', 'deposit', 500)
        self.store.review_cash('paper', last['as_of'])
        self.assertAlmostEqual(self.report()['ALL']['metrics']['return_rate'], .12)

    def test_range_returns_rebase_without_outside_flows_or_borrowed_prices(self):
        self.record('2026-08-01T20:00:00Z', 1000)
        self.record('2026-09-25T20:00:00Z', 2100)
        self.record('2026-10-01T20:00:00Z', 2200)
        last = self.record('2026-10-07T20:00:00Z', 2420)
        self.flow('2026-09-01T20:00:00Z', 'deposit', 1000)
        self.store.review_cash('paper', last['as_of'])
        week = self.report()['1W']
        self.assertEqual(week['start'], '2026-10-01T20:00:00+00:00')
        self.assertEqual(week['metrics']['profit'], 220)
        self.assertEqual(week['metrics']['net_flow'], 0)
        self.assertAlmostEqual(week['metrics']['return_rate'], .1)
        self.assertEqual(self.report()['ALL']['metrics']['profit'], 420)

    def test_reset_keeps_first_intraday_post_reset_observation(self):
        self.record('2026-10-01T13:30:00Z', 1000)
        baseline = self.record('2026-10-01T15:00:00Z', 5000)
        last = self.record('2026-10-01T20:00:00Z', 5100)
        self.flow('2026-10-01T15:00:00Z', 'reset', 0)
        self.store.review_cash('paper', last['as_of'])
        report = self.report()['ALL']
        self.assertEqual(report['start'], baseline['as_of'])
        self.assertEqual(report['metrics']['profit'], 100)
        self.assertAlmostEqual(report['metrics']['return_rate'], .02)

    def test_new_flow_correction_invalidates_verified_prefix_and_other_accounts_are_separate(self):
        self.record('2026-10-01T20:00:00Z', 1000)
        last = self.record('2026-10-02T20:00:00Z', 1100)
        self.store.review_cash('paper', last['as_of'])
        self.record('2026-10-02T20:00:00Z', 999999, scope='other')
        ident = self.flow('2026-10-02T20:00:00Z', 'deposit', 50)
        self.assertIsNone(self.report()['ALL']['verified'])
        self.store.remove_cash_event('paper', ident)
        self.assertIsNone(self.report()['ALL']['verified'])
        self.assertEqual(self.report()['ALL']['points'][-1]['equity'], 1100)

    def test_single_sample_and_unreviewed_history_do_not_invent_investment_results(self):
        last = self.record('2026-10-01T20:00:00Z', 1000)
        self.store.review_cash('paper', last['as_of'])
        for range in self.report().values():
            self.assertIsNone(range['metrics']['return_rate'])
            self.assertIsNone(range['metrics']['profit'])
        self.record('2026-10-02T20:00:00Z', 1100)
        self.assertIsNone(self.report()['ALL']['verified'])

    def test_calendar_month_and_new_year_follow_new_york_date(self):
        self.assertEqual(cutoff('2026-03-31T20:00:00Z', '1M').date().isoformat(), '2026-02-28')
        self.assertEqual(cutoff('2026-01-01T01:00:00Z', 'YTD').date().isoformat(), '2025-01-01')

    def test_partial_position_pnl_is_unknown_not_zero_or_legacy_unrealized(self):
        self.assertEqual(position_summary([])['position_pnl'], 0)
        held = [dict(quantity=1, position_pnl=20), dict(quantity=1, unrealized_pnl=99)]
        self.assertIsNone(position_summary(held)['position_pnl'])
        self.assertEqual(position_summary(held)['pnl_covered_count'], 1)
        held[1]['position_pnl'] = -5
        self.assertEqual(position_summary(held)['position_pnl'], 15)


if __name__ == '__main__':
    unittest.main()
