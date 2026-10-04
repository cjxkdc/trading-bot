from dataclasses import FrozenInstanceError, replace
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from app.assembly import build_portfolio_runner, default_portfolio_config, open_paper_session
from framework.decisions import decide
from framework.execution import ExecutionBlocked
from framework.portfolio import PortfolioContext, PortfolioEngine, StockTarget
from platforms.data.snapshot import SnapshotFile, demo_snapshot
from test_tech_profit_momentum import stock, load_adapter


class RetainTestStrategy:
    requires_context = True

    def select_with_context(self, stocks, context):
        return tuple(StockTarget(s.asset_id, .19, 0) for s in stocks if s.asset_id in context.held_asset_ids)


class PortfolioContextTests(unittest.TestCase):
    def test_holdings_are_immutable_canonical_and_not_a_broker(self):
        context = PortfolioContext(date(2020, 1, 2), ("B", "A"))
        self.assertEqual(context.held_asset_ids, ("A", "B"))
        with self.assertRaises(FrozenInstanceError):
            context.held_asset_ids = ()
        with self.assertRaises(ValueError):
            PortfolioContext(date(2020, 1, 2), ["A"])
        with self.assertRaises(ValueError):
            PortfolioContext(date(2020, 1, 2), ("A", "A"))

    def test_context_required_is_different_from_an_empty_account(self):
        engine = PortfolioEngine(RetainTestStrategy())
        with self.assertRaisesRegex(ValueError, "requires portfolio context"):
            engine.plan([stock("A")], date(2020, 1, 2))
        self.assertEqual(engine.plan([stock("A")], date(2020, 1, 2), PortfolioContext(date(2020, 1, 2), ())), ())

    def test_future_or_stale_context_is_rejected(self):
        for stamp in (date(2020, 1, 3), date(2019, 12, 1)):
            with self.assertRaises(ValueError):
                PortfolioEngine(RetainTestStrategy()).plan([stock("A")], date(2020, 1, 2), PortfolioContext(stamp, ("A",)))

    def test_holdings_change_decision_and_match_local_qc_helper(self):
        adapter = load_adapter()
        engine = PortfolioEngine(RetainTestStrategy())
        a, b = (PortfolioContext(date(2020, 1, 2), (s,)) for s in ("A", "B"))
        rows = [stock("A"), stock("B")]
        cloud = adapter.make_decision(engine, rows, date(2020, 1, 2), a)
        from app.assembly import portfolio_spec
        local = decide(engine, rows, date(2020, 1, 2), portfolio_spec(adapter.strategy_configuration()), context=a)
        self.assertEqual(local, cloud)
        self.assertNotEqual(local.decision_id, decide(engine, rows, date(2020, 1, 2), local.record["strategy"], context=b).decision_id)
        self.assertEqual([t.asset_id for t in local.targets], ["A"])

    def test_account_mismatch_blocks_before_orders_and_frozen_context_can_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = demo_snapshot()
            data["portfolio_context"] = {"as_of": "2026-09-01", "held_asset_ids": []}
            path = root / "snapshot.json"
            path.write_text(json.dumps(data))
            snapshot = SnapshotFile(path).load()
            cfg = default_portfolio_config()
            runner = build_portfolio_runner(cfg)
            with open_paper_session(cfg, snapshot, root / "state", runner.fees) as (broker, journal):
                wrong = replace(snapshot, portfolio_context=PortfolioContext(date(2026, 9, 1), ("DEMO-00",)))
                with self.assertRaisesRegex(ExecutionBlocked, "context differs"):
                    runner.execute(runner.plan(wrong), wrong, broker, journal)
                self.assertEqual(broker.account().positions, ())
                decision = runner.plan(snapshot)
                first = runner.execute(decision, snapshot, broker, journal)
                second = runner.execute(decision, snapshot, broker, journal)
                self.assertEqual(first, second)  # Original empty context stays frozen after fills.
                self.assertEqual(len(broker.account().positions), 5)

    def test_bundled_context_contract_produces_the_same_decision_record(self):
        from test_composition import bundle_namespace
        from app.assembly import portfolio_spec
        cfg = default_portfolio_config()
        namespace = bundle_namespace(cfg)
        remote_context = namespace["PortfolioContext"](date(2020, 1, 2), ("A",))
        local_context = PortfolioContext(date(2020, 1, 2), ("A",))
        rows = [stock("A"), stock("B")]
        remote_engine = namespace["PortfolioEngine"](RetainTestStrategy())
        cloud = namespace["make_decision"](remote_engine, rows, date(2020, 1, 2), remote_context)
        local = decide(PortfolioEngine(RetainTestStrategy()), rows, date(2020, 1, 2),
                       portfolio_spec(cfg["strategy"]), context=local_context)
        self.assertEqual(local.record, cloud.record)
        self.assertEqual(local.decision_id, cloud.decision_id)


if __name__ == "__main__":
    unittest.main()
