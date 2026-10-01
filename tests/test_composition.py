"""Behavioral contracts for interchangeable modules and reproducible QC exports."""
import ast
from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import assembly
from assembly import (build_portfolio_runner, default_portfolio_config,
                      open_paper_session, portfolio_spec, prepare_portfolio)
from bot import export_backtest, run_paper
from framework.configuration import load_config, normalize_config
from framework.execution import ExecutionBlocked
from framework.portfolio import PortfolioEngine
from platforms.brokers.paper import PaperBroker
from platforms.data.snapshot import SnapshotFile, demo_snapshot
from test_tech_profit_momentum import load_adapter
from tools.build_quantconnect import build


ROOT = Path(__file__).resolve().parents[1]


def bundle_namespace(config):
    api = types.ModuleType("AlgorithmImports")
    api.QCAlgorithm = type("QCAlgorithm", (), {})
    namespace = {"__name__": "composed_qc_test"}
    with patch.dict(sys.modules, {"AlgorithmImports": api}):
        exec(compile(build(config), "qc_bundle", "exec"), namespace)
    return namespace


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot.json"
        self.snapshot.write_text(json.dumps(demo_snapshot()), encoding="utf-8")

    def config_file(self, cfg, name="config.json"):
        path = self.root / name
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_switch_strategy_by_config_reuses_runner_and_changes_selection(self):
        _, first, snapshot, momentum = prepare_portfolio(ROOT / "portfolio_config.json", self.snapshot)
        _, second, _, control = prepare_portfolio(ROOT / "configs/equal_weight_control.json", self.snapshot)
        self.assertIs(type(first), type(second))
        self.assertEqual({t.asset_id for t in momentum.targets},
                         {"DEMO-03", "DEMO-04", "DEMO-05", "DEMO-06", "DEMO-07"})
        self.assertEqual([t.asset_id for t in control.targets], [f"DEMO-{i:02d}" for i in range(5)])
        self.assertEqual(momentum, first.plan(snapshot))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["snapshot.json"])

    def test_new_data_and_broker_manifests_work_without_changing_the_runner(self):
        module = types.ModuleType("platforms.test_connectors")

        class ReversedData(SnapshotFile):
            def load(self):
                value = super().load()
                return replace(value, stocks=tuple(reversed(value.stocks)))

        class OtherConnector:
            mode, state_files = "paper", ("other.sqlite",)

            def initialize(self, state_dir, initial_cash):
                PaperBroker.create(state_dir / self.state_files[0], initial_cash)

            def connect(self, state_dir, snapshot, fees):
                return PaperBroker(state_dir / self.state_files[0], snapshot.quotes,
                                   snapshot.execution_time, fees)

        module.ReversedData, module.OtherConnector = ReversedData, OtherConnector
        folder = self.root / "plugins"
        folder.mkdir()
        entries = assembly.platform_catalog()
        entries.update({
            "reversed": {"type": "reversed", "kind": "snapshot_data", "version": "1",
                         "entrypoint": "platforms.test_connectors:ReversedData",
                         "input_schema": "us_technology_daily_v1"},
            "other": {"type": "other", "kind": "broker", "version": "1",
                      "entrypoint": "platforms.test_connectors:OtherConnector"}})
        for key, entry in entries.items():
            (folder / (key + ".json")).write_text(json.dumps(entry), encoding="utf-8")
        original_catalog = assembly.platform_catalog
        cfg = default_portfolio_config()
        cfg.update(data={"type": "reversed"}, broker={"type": "other"})
        config = self.config_file(cfg)
        ordinary = run_paper(self.snapshot, ROOT / "portfolio_config.json", self.root / "ordinary")
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(
                assembly, "platform_catalog", lambda: original_catalog(folder)):
            alternate = run_paper(self.snapshot, config, self.root / "alternate")
            repeated = run_paper(self.snapshot, config, self.root / "alternate")
        self.assertEqual(alternate, repeated)
        self.assertEqual(alternate["decision_id"], ordinary["decision_id"])
        self.assertEqual(alternate["account"]["cash"], ordinary["account"]["cash"])
        self.assertEqual(alternate["account"]["positions"], ordinary["account"]["positions"])
        self.assertTrue((self.root / "alternate/other.sqlite").is_file())
        self.assertFalse((self.root / "alternate/paper.sqlite").exists())

    def test_changed_data_schema_stops_before_account_creation(self):
        cfg = default_portfolio_config()
        cfg["data"] = {"type": "wrong_schema"}
        entries = assembly.platform_catalog()
        entries["wrong_schema"] = dict(entries["snapshot_file"], input_schema="crypto_v1")
        with patch.object(assembly, "platform_catalog", return_value=entries):
            with self.assertRaisesRegex(ValueError, "schemas"):
                run_paper(self.snapshot, self.config_file(cfg), self.root / "state")
        self.assertFalse((self.root / "state").exists())

    def test_live_connector_is_rejected_before_initialization_or_connection(self):
        cfg, runner, snapshot, _ = prepare_portfolio(ROOT / "portfolio_config.json", self.snapshot)
        connector = Mock(mode="live")
        with patch.object(assembly, "_build_platform", return_value=connector):
            with self.assertRaisesRegex(ValueError, "paper connections only"):
                with open_paper_session(cfg, snapshot, self.root / "state", runner.fees):
                    self.fail("opened a live session")
        connector.initialize.assert_not_called()
        connector.connect.assert_not_called()
        self.assertFalse((self.root / "state").exists())

    def test_strategy_change_cannot_reuse_a_completed_account_cycle(self):
        state = self.root / "state"
        run_paper(self.snapshot, ROOT / "portfolio_config.json", state)
        with self.assertRaisesRegex(ExecutionBlocked, "frozen"):
            run_paper(self.snapshot, ROOT / "configs/equal_weight_control.json", state)

    def test_local_and_exported_custom_parameters_produce_identical_decisions(self):
        cfg = default_portfolio_config()
        cfg["strategy"].update(count=3, weight=.25, lookback=126, skip=5, require_profit=False)
        cfg["limits"].update(max_gross="0.75", max_weight="0.25")
        cfg["backtest"].update(start="2021-01-01", end="2022-12-31")
        local = build_portfolio_runner(cfg)
        namespace = bundle_namespace(cfg)
        exported_cfg = namespace["strategy_configuration"]()
        remote_engine = namespace["PortfolioEngine"](
            namespace["build_portfolio_strategy"](exported_cfg), .75, .25)
        snapshot = SnapshotFile(self.snapshot).load()
        cloud = namespace["make_decision"](remote_engine, snapshot.stocks, date(2026, 9, 1))
        self.assertEqual(local.plan(snapshot).record, cloud.record)
        self.assertEqual(namespace["START"], (2021, 1, 1))
        self.assertEqual(namespace["END"], (2022, 12, 31))
        self.assertEqual(namespace["_RUN_CONFIG"]["limits"]["max_weight"], "0.25")

    def test_omitted_defaults_are_recorded_identically_to_explicit_parameters(self):
        implicit = portfolio_spec({"type": "tech_profit_momentum"})
        explicit = portfolio_spec(default_portfolio_config()["strategy"])
        self.assertEqual(implicit, explicit)

    def test_export_records_unrun_status_and_excludes_connection_settings(self):
        cfg = default_portfolio_config()
        cfg["broker"]["token"] = "NOT-A-REAL-SECRET-DO-NOT-EXPORT"
        cfg["data"]["token"] = "NOT-A-REAL-SECRET-DO-NOT-EXPORT"
        output = self.root / "experiment"
        result = export_backtest(self.config_file(cfg), output)
        self.assertEqual(result["status"], "exported_not_run")
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertNotIn("broker", manifest["configuration"])
        self.assertNotIn("DO-NOT-EXPORT", (output / "main.py").read_text())
        with self.assertRaises(FileExistsError):
            export_backtest(self.config_file(cfg), output)

    def test_unsupported_qc_fee_model_cannot_silently_change_costs(self):
        cfg = default_portfolio_config()
        cfg["fees"]["bps"] = "3"
        with self.assertRaisesRegex(ValueError, "fixed per-order"):
            export_backtest(self.config_file(cfg), self.root / "experiment")
        self.assertFalse((self.root / "experiment").exists())

    def test_string_false_does_not_silently_enable_profit_filter(self):
        cfg = default_portfolio_config()
        cfg["strategy"]["require_profit"] = "false"
        with self.assertRaisesRegex(ValueError, "boolean"):
            prepare_portfolio(self.config_file(cfg), self.snapshot)

    def test_config_inheritance_has_no_hidden_parameter_merge_and_rejects_cycles(self):
        control = normalize_config(load_config(ROOT / "configs/equal_weight_control.json"))
        self.assertNotIn("lookback", control["strategy"])
        self.assertEqual(control["backtest"], default_portfolio_config()["backtest"])
        circular = self.config_file({"extends": "config.json"})
        with self.assertRaisesRegex(ValueError, "cycle"):
            load_config(circular)

    def test_strategies_cannot_import_platforms_runners_or_broker_sdks(self):
        for path in (ROOT / "strategies").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                forbidden = {"platforms", "assembly", "bot", "moomoo", "futu", "tigeropen", "AlgorithmImports"}
                self.assertFalse(any(name.split(".")[0] in forbidden for name in names), str(path))


class HistoryRequirementsTests(unittest.TestCase):
    def test_qc_requests_strategy_history_and_keeps_profit_filter_in_strategy(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas is required for the historical adapter contract")
        module = load_adapter()
        module.STRATEGY_PARAMETERS = dict(module.STRATEGY_PARAMETERS, lookback=504)
        algo = module.TechProfitMomentumAlgorithm()
        algo.time, algo.qqq = datetime(2020, 1, 2), "QQQ"
        days = pd.bdate_range(end="2019-12-31", periods=505) + pd.Timedelta(hours=16)
        requests = []

        class Symbol(str):
            @property
            def value(self):
                return str(self)

        def history(symbols, count, normalization):
            requests.append((count, normalization))
            index = pd.MultiIndex.from_product([symbols, days[-count:]], names=["symbol", "time"])
            return pd.DataFrame({"close": 100.0, "volume": 200000.0}, index=index)

        algo._history = history
        rows = algo._load_candidates({"A": (Symbol("A"), "software", -1)}, date(2019, 12, 31))
        self.assertEqual(len(rows), 1)  # Provider keeps it; the selected strategy rejects it.
        self.assertEqual(len(rows[0].adjusted_closes), 505)
        self.assertEqual(len(rows[0].dollar_volumes), 20)
        self.assertEqual(requests, [(505, "adjusted"), (505, "adjusted"), (20, "raw")])
        self.assertEqual(PortfolioEngine(module.build_portfolio_strategy(
            module.strategy_configuration())).plan(rows, date(2020, 1, 2)), ())


if __name__ == "__main__":
    unittest.main()
