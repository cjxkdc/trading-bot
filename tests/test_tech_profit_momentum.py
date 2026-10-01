import importlib.util
import math
import sys
import types
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from framework.portfolio import PortfolioEngine, StockSnapshot, StockTarget
from strategies.tech_profit_momentum import TechProfitMomentum
from tools.build_quantconnect import build


def stock(sid="A", score=1, industry="software", **changes):
    prices = [100.0] * 253
    prices[-22] = 100 * (1 + score)
    result = StockSnapshot(sid, sid, date(2019, 12, 31), date(2019, 12, 31),
                           industry, 1_000_000, tuple(prices), (20_000_000.0,) * 20)
    return replace(result, **changes)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = TechProfitMomentum()

    def test_skips_latest_21_sessions_and_uses_253_closes(self):
        s = stock()
        prices = list(s.adjusted_closes)
        prices[-21:] = [0.01] * 21
        target, = self.strategy.select([replace(s, adjusted_closes=tuple(prices))])
        self.assertEqual(target.score, 1)

    def test_recent_ipo_or_missing_liquidity_is_ineligible(self):
        self.assertEqual(self.strategy.select([
            stock("A", adjusted_closes=(1.0,) * 252),
            stock("B", dollar_volumes=(20_000_000,) * 19)]), ())

    def test_profit_must_be_positive_finite(self):
        rows = [stock(str(i), operating_income_ttm=p) for i, p in enumerate((0, -1, math.nan, math.inf))]
        self.assertEqual(self.strategy.select(rows), ())

    def test_liquidity_is_20_day_mean_not_latest_day(self):
        liquid = stock("A", dollar_volumes=(20_000_000,) * 19 + (1,))
        illiquid = stock("B", dollar_volumes=(1,) * 19 + (20_000_000,))
        self.assertEqual([t.asset_id for t in self.strategy.select([liquid, illiquid])], ["A"])

    def test_industry_cap_five_slots_and_stable_ties(self):
        rows = [stock(str(i), 10-i, str(i//3)) for i in range(9)]
        targets = self.strategy.select(list(reversed(rows)))
        self.assertEqual([t.asset_id for t in targets], ["0", "1", "3", "4", "6"])
        self.assertAlmostEqual(sum(t.weight for t in targets), 0.95)
        self.assertEqual([t.asset_id for t in self.strategy.select([stock("B"), stock("A")])], ["A", "B"])

    def test_unfilled_slots_stay_cash_and_negative_momentum_is_allowed(self):
        targets = self.strategy.select([stock("A", -0.2), stock("B", -0.5)])
        self.assertAlmostEqual(sum(t.weight for t in targets), 0.38)
        self.assertTrue(all(t.score < 0 for t in targets))

    def test_nan_prices_or_unknown_industry_are_ineligible(self):
        self.assertEqual(self.strategy.select([
            stock("A", adjusted_closes=(math.nan,) * 253), stock("B", industry="")]), ())

    def test_controlled_variants_remove_only_declared_filters(self):
        rows = [stock(str(i), 4-i, operating_income_ttm=-1) for i in range(4)]
        self.assertEqual(len(TechProfitMomentum(require_profit=False, industry_limit=5).select(rows)), 4)
        self.assertEqual(len(TechProfitMomentum(require_profit=False).select(rows)), 2)

    def test_framework_blocks_future_information_and_duplicate_assets(self):
        engine = PortfolioEngine(self.strategy)
        for rows in ([stock(known_on=date(2020, 1, 2))],
                     [stock(price_as_of=date(2020, 1, 2))], [stock(), stock()]):
            with self.assertRaises(ValueError):
                engine.plan(rows, date(2020, 1, 2))

    def test_framework_blocks_bad_strategy_targets(self):
        for targets in ((StockTarget("unknown", .19, 1),),
                        (StockTarget("A", -.1, 1),),
                        (StockTarget("A", .2, 1),),
                        (StockTarget("A", math.nan, 1),),
                        (StockTarget("A", .19, 1),) * 2):
            with self.assertRaises(ValueError):
                PortfolioEngine(Mock(select=lambda _: targets)).plan([stock()], date(2020, 1, 2))


def load_adapter():
    """A narrow host double tests our control flow, not LEAN API compatibility."""
    api = types.ModuleType("AlgorithmImports")
    api.QCAlgorithm = type("QCAlgorithm", (), {})
    api.Universe = types.SimpleNamespace(UNCHANGED=object())
    api.MorningstarSectorCode = types.SimpleNamespace(TECHNOLOGY=311)
    api.PortfolioTarget = lambda symbol, weight: (symbol, weight)
    api.OrderStatus = types.SimpleNamespace(INVALID="invalid")
    api.DataNormalizationMode = types.SimpleNamespace(RAW="raw", ADJUSTED="adjusted")
    path = Path(__file__).resolve().parents[1] / "platforms/quantconnect/adapter.py"
    spec = importlib.util.spec_from_file_location("qc_adapter_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"AlgorithmImports": api}):
        spec.loader.exec_module(module)
    return module


class AdapterTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_adapter()

    def make_algo(self):
        algo = self.module.TechProfitMomentumAlgorithm()
        algo.time = datetime(2020, 1, 2)
        # Include New Year's holiday in the calendar double.
        algo._hours = types.SimpleNamespace(is_date_open=lambda dt: dt.weekday() < 5 and dt.date() != date(2020, 1, 1))
        algo.is_warming_up = False
        algo._fact_snapshots = {datetime(2019, 12, 31): {"A": ("A-symbol", "software", 1)}}
        algo._last_month = None
        algo._pending = None
        algo._engine = PortfolioEngine(TechProfitMomentum())
        algo._completed = 0
        algo.log = Mock()
        algo.plot = Mock()
        algo._freeze_facts = Mock(return_value={"A": ("A-symbol", "software", -1)})
        algo._load_candidates = Mock(return_value=[stock()])
        return algo

    def test_month_start_uses_frozen_prior_facts_not_new_income(self):
        algo = self.make_algo()
        self.assertEqual(algo._select_universe([]), ["A-symbol"])
        facts, day, known_on = algo._load_candidates.call_args.args
        self.assertEqual(facts["A"][2], 1)
        self.assertEqual(day, date(2019, 12, 31))
        self.assertEqual(known_on, date(2019, 12, 31))
        self.assertEqual(algo._fact_snapshots[datetime(2020, 1, 2)]["A"][2], -1)
        algo.time = datetime(2020, 1, 3)
        algo._select_universe([])
        self.assertEqual(algo._load_candidates.call_count, 1)

    def test_warmup_and_midmonth_start_do_not_trade(self):
        algo = self.make_algo()
        algo.is_warming_up = True
        algo.time = datetime(2019, 12, 27)
        algo._select_universe([])
        algo._load_candidates.assert_not_called()
        algo = self.make_algo()
        algo.time = datetime(2020, 1, 15)
        algo._select_universe([])
        algo._load_candidates.assert_not_called()

    def test_missing_prior_snapshot_fails_explicitly(self):
        algo = self.make_algo()
        algo._fact_snapshots = {datetime(2019, 12, 30): {"A": ("A-symbol", "software", 1)}}
        with self.assertRaisesRegex(RuntimeError, "Missing timely fundamentals"):
            algo._select_universe([])
        self.assertIsNone(algo._last_month)

    def test_september_2020_monday_month_end_uses_saturday_delivery(self):
        algo = self.make_algo()
        algo._fact_snapshots = {}
        algo._freeze_facts = Mock(return_value={"A": ("A-symbol", "software", 1)})
        for day in (27, 28, 29):
            algo.time = datetime(2020, 8, day)
            algo._select_universe([])
        algo._load_candidates.assert_not_called()
        # No callback on Monday Aug 31: Friday data was delivered on Saturday.
        algo.time = datetime(2020, 9, 1)
        algo._select_universe([])
        _, signal_day, known_on = algo._load_candidates.call_args.args
        self.assertEqual((signal_day, known_on), (date(2020, 8, 31), date(2020, 8, 29)))
        self.assertEqual(algo._pending[0], date(2020, 9, 1))

    def test_weekend_callback_prepares_monday_and_does_not_trade_early(self):
        algo = self.make_algo()
        algo._fact_snapshots = {datetime(2020, 1, 31): {"A": ("A-symbol", "software", 1)}}
        algo.time = datetime(2020, 2, 1)
        self.assertEqual(algo._select_universe([]), ["A-symbol"])
        self.assertEqual(algo._pending[0], date(2020, 2, 3))
        algo.set_holdings = Mock()
        algo.on_data(object())
        algo.set_holdings.assert_not_called()
        # Even a second weekend delivery must not replace or execute the plan.
        algo.time = datetime(2020, 2, 2)
        algo._select_universe([])
        self.assertEqual(algo._load_candidates.call_count, 1)

    def test_warmup_boundary_can_prepare_after_holiday_without_orders(self):
        algo = self.make_algo()
        algo.time = datetime(2020, 1, 1)
        algo.is_warming_up = True
        algo._select_universe([])
        self.assertEqual(algo._pending[0], date(2020, 1, 2))
        algo.set_holdings = Mock()
        algo.on_data(object())
        algo.set_holdings.assert_not_called()

    def test_snapshot_after_month_end_midnight_is_not_backdated(self):
        algo = self.make_algo()
        algo._fact_snapshots = {datetime(2019, 12, 31, 16): {"A": ("A-symbol", "software", 1)}}
        with self.assertRaisesRegex(RuntimeError, "Missing timely fundamentals"):
            algo._select_universe([])

    def test_2020_delivery_calendar_produces_all_twelve_rebalances(self):
        algo = self.make_algo()
        holidays = {date(2019, 12, 25), date(2020, 1, 1), date(2020, 1, 20),
                    date(2020, 2, 17), date(2020, 4, 10), date(2020, 5, 25),
                    date(2020, 7, 3), date(2020, 9, 7), date(2020, 11, 26), date(2020, 12, 25)}
        algo._hours = types.SimpleNamespace(is_date_open=lambda dt: dt.weekday() < 5 and dt.date() not in holidays)
        algo._fact_snapshots = {}
        algo._freeze_facts = Mock(return_value={"A": ("A-symbol", "software", 1)})
        algo._load_candidates = lambda facts, signal, known: [stock(known_on=known, price_as_of=signal)]
        algo.portfolio = []
        algo._fresh = lambda data, symbol: True
        executed = []
        algo.set_holdings = lambda *a, **k: executed.append(algo.time.date())
        day = date(2019, 12, 21)
        while day <= date(2020, 12, 31):
            algo.time = datetime.combine(day, datetime.min.time())
            algo.is_warming_up = day <= date(2020, 1, 1)
            # Replay the documented next-calendar-day delivery pattern, not
            # an assumed callback on every exchange session.
            if algo._is_session(day - timedelta(days=1)):
                algo._select_universe([])
                algo.on_data(object())
            if algo._is_session(day):
                algo.time = algo.time.replace(hour=9, minute=35)
                algo.on_data(object())
            day += timedelta(days=1)
        expected = [date(2020, m, d) for m, d in enumerate((2, 3, 2, 1, 1, 1, 1, 3, 1, 1, 2, 1), 1)]
        self.assertEqual(executed, expected)
        self.assertEqual(algo._completed, 12)

    def test_batch_orders_reweight_existing_and_liquidate_exits_once(self):
        algo = self.make_algo()
        algo._select_universe([])
        algo.time = datetime(2020, 1, 2, 9, 35)
        algo.portfolio = [types.SimpleNamespace(key="OLD", value=types.SimpleNamespace(invested=True))]
        algo._fresh = Mock(return_value=True)
        algo.set_holdings = Mock()
        algo.on_data(object())
        algo.on_data(object())
        algo.set_holdings.assert_called_once()
        self.assertEqual(algo.set_holdings.call_args.args[0], [("A-symbol", .19)])
        self.assertTrue(algo.set_holdings.call_args.kwargs["liquidate_existing_holdings"])

    def test_stale_existing_holdings_block_trade(self):
        algo = self.make_algo()
        algo._select_universe([])
        algo.time = datetime(2020, 1, 2, 10, 0)
        algo.portfolio = [types.SimpleNamespace(key="OLD", value=types.SimpleNamespace(invested=True))]
        algo._fresh = lambda data, symbol: symbol != "OLD"
        algo.set_holdings = Mock()
        with self.assertRaisesRegex(RuntimeError, "stale or halted"):
            algo.on_data(object())
        algo.set_holdings.assert_not_called()

    def test_empty_selection_liquidates_and_live_mode_is_blocked(self):
        algo = self.make_algo()
        algo._pending = (date(2020, 1, 2), (), {})
        algo.time = datetime(2020, 1, 2, 9, 35)
        algo.portfolio = []
        algo.liquidate = Mock()
        algo.on_data(object())
        algo.liquidate.assert_called_once()
        algo.live_mode = True
        with self.assertRaisesRegex(RuntimeError, "backtest only"):
            algo.initialize()


class BundleTests(unittest.TestCase):
    def test_paste_ready_bundle_matches_sources_and_has_no_local_imports(self):
        import ast
        source = build()
        path = Path(__file__).resolve().parents[1] / "platforms/quantconnect/main.py"
        self.assertEqual(source, path.read_text(encoding="utf-8"))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], {"framework", "strategies", "platforms"})


class HistoryAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import pandas as pd
        except ImportError:
            raise unittest.SkipTest("History table contract tests require pandas (included on QC)")
        cls.pd, cls.module = pd, load_adapter()

    def frame(self, symbol, days, closes, volumes):
        index = self.pd.MultiIndex.from_product([[symbol], days], names=["symbol", "time"])
        return self.pd.DataFrame({"close": closes, "volume": volumes}, index=index)

    def test_daily_stamps_and_future_data(self):
        algorithm = self.module.TechProfitMomentumAlgorithm
        # Midnight and precise close stamps identify the same completed session.
        for stamp in (datetime(2019, 12, 31, 16), datetime(2020, 1, 1)):
            frame = self.frame("A", [stamp], [100], [1000])
            rows = algorithm._daily_rows(frame, "A", datetime(2020, 1, 2))
            self.assertEqual(list(rows), [date(2019, 12, 31)])
        frame = self.frame("A", [datetime(2020, 1, 2, 16)], [100], [1000])
        with self.assertRaisesRegex(ValueError, "Future history"):
            algorithm._daily_rows(frame, "A", datetime(2020, 1, 2))

    def test_complete_session_alignment_and_raw_split_dollar_volume(self):
        algo = self.module.TechProfitMomentumAlgorithm()
        algo.time, algo.qqq = datetime(2020, 1, 2), "QQQ"
        days = self.pd.bdate_range(end="2019-12-31", periods=253) + self.pd.Timedelta(hours=16)
        prices = [100] * 253
        prices[-22] = 120
        good = self.frame("A", days, prices, [1000] * 253)
        missing = self.frame("B", days.delete(5), [100] * 252, [1000] * 252)
        adjusted = self.pd.concat([good, missing])
        # A 2:1 split changes the raw price and volume, not the dollar-volume gate.
        raw = self.pd.concat([self.frame(symbol, days[-20:], [200] * 10 + [100] * 10,
                                        [100_000] * 10 + [200_000] * 10) for symbol in ("A", "B")])
        reference = self.frame("QQQ", days, [100] * 253, [1000] * 253)
        algo._history = lambda symbols, count, mode: reference if symbols == ["QQQ"] else (
            raw if mode == "raw" else adjusted)
        # Real Symbol objects are represented by strings in this host contract test.
        class Symbol(str):
            @property
            def value(self):
                return str(self)
        rows = algo._load_candidates({"A": (Symbol("A"), "software", 1),
                                      "B": (Symbol("B"), "software", 1)}, date(2019, 12, 31))
        self.assertEqual([row.asset_id for row in rows], ["A"])
        self.assertEqual(rows[0].dollar_volumes, (20_000_000,) * 20)
        target, = PortfolioEngine(TechProfitMomentum()).plan(rows, date(2020, 1, 2))
        self.assertAlmostEqual(target.score, .2)

    def test_fundamental_values_are_copied_not_retained_as_live_getters(self):
        ns = types.SimpleNamespace
        f = ns(has_fundamental_data=True, asset_classification=ns(
            morningstar_sector_code=311, morningstar_industry_code=311101),
            security_reference=ns(security_type="ST00000001", is_depositary_receipt=False, is_primary_share=True),
            financial_statements=ns(income_statement=ns(operating_income=ns(twelve_months=100))),
            symbol=ns(id="stable-sid"))
        algo = self.module.TechProfitMomentumAlgorithm()
        frozen = algo._freeze_facts([f])
        f.financial_statements.income_statement.operating_income.twelve_months = -100
        f.asset_classification.morningstar_industry_code = 999
        self.assertEqual(frozen["stable-sid"][1:], ("311101", 100.0))


if __name__ == "__main__":
    unittest.main()
