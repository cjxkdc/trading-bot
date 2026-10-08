import unittest
from datetime import date, timedelta
from app.research import backtest, signal, evaluate_benchmark


class ResearchTests(unittest.TestCase):
    def test_signal_does_not_read_future(self):
        s = {"type": "ma", "fast": 1, "slow": 2}
        self.assertEqual(signal([1, 2, 999], 1, s), signal([1, 2, 0.1], 1, s))

    def test_signal_executes_after_price_jump_not_before(self):
        rows = [(date(2020, 1, 1) + timedelta(days=i), p) for i, p in enumerate([2, 1, 10, 10])]
        r = backtest(rows, {"type": "ma", "fast": 1, "slow": 2},
                     ["2020-01-03", "2020-01-04"], 1000, 1, 0, 2)
        self.assertEqual(r["final_equity"], 1000)
        self.assertEqual(r["curve"][0]["target"], 0)
        self.assertEqual(r["curve"][1]["target"], 1)

    def test_passive_benchmark_does_not_require_an_installed_hold_plugin(self):
        from unittest.mock import patch
        rows = [(date(2020, 1, 1) + timedelta(days=i), 10) for i in range(4)]
        cfg = dict(periods={'validation': ['2020-01-02', '2020-01-04']},
                   initial_capital=1000, max_weight=1, cost_bps_one_way=100, minimum_sessions=2)
        with patch('app.research.build_strategy', side_effect=AssertionError('No strategy plugin needed')):
            result = evaluate_benchmark(rows, cfg, 'validation')
        self.assertAlmostEqual(result['final_equity'], 1000 / 1.01)
        self.assertEqual(result['trades'], 1)

    def test_entry_fee_is_funded_without_borrowing(self):
        rows = [(date(2020, 1, 1) + timedelta(days=i), 10) for i in range(4)]
        r = backtest(rows, {"type": "hold"}, ["2020-01-02", "2020-01-04"], 1000, 1, 100, 2)
        self.assertAlmostEqual(r["final_equity"], 1000 / 1.01)
        self.assertEqual(r["trades"], 1)


if __name__ == "__main__":
    unittest.main()
