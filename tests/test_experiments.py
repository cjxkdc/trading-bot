"""Synthetic QC-shaped fixtures verify bookkeeping, not strategy performance."""
import json
from pathlib import Path
import tempfile
import unittest

from bot import export_backtest
from framework.experiments import compare_results, import_result, read_json, verified_record
from platforms.quantconnect.results import parse_result

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return {"success": True, "backtest": {"backtestId": "synthetic-test-only", "status": "Completed.",
            "completed": True, "progress": 1, "error": "", "statistics": {
                "Net Profit": "140.67%", "Compounding Annual Return": "24.50%",
                "Drawdown": "38.2%", "Sharpe Ratio": "1.25", "Total Orders": "73"},
            "runtimeStatistics": {"Net Profit": "$58,699.11", "Return": "140.67%",
                                  "Research start": "2020-01-01", "Research end": "2020-12-31"}}}


class ResultsParserTests(unittest.TestCase):
    def test_total_return_and_annual_return_and_realized_dollars_are_distinct(self):
        result = parse_result(fixture())
        self.assertAlmostEqual(result["metrics"]["total_return"], 1.4067)
        self.assertAlmostEqual(result["metrics"]["annual_return"], .245)
        self.assertEqual(result["completion"], "reported_complete")

    def test_error_wins_over_completed_flag(self):
        for changes in ({"error": "Runtime failure"}, {"status": "Runtime Error"}, {"hasInitializeError": True}):
            data = fixture()
            data["backtest"].update(changes)
            self.assertEqual(parse_result(data)["completion"], "failed")

    def test_incomplete_and_missing_completion_evidence_are_visible(self):
        data = fixture()
        data["backtest"]["progress"] = .4
        self.assertEqual(parse_result(data)["completion"], "incomplete")
        download = {"Statistics": data["backtest"]["statistics"]}
        self.assertEqual(parse_result(download)["completion"], "unverified")
        download["Progress"] = 1
        self.assertEqual(parse_result(download)["completion"], "unverified")

    def test_lean_packet_uses_historical_dates_and_request_time_is_ignored(self):
        stats = fixture()["backtest"]["statistics"]
        packet = {"PeriodStart": "2020-01-01T00:00:00", "PeriodFinish": "2020-12-31T23:59:59",
                  "DateRequested": "2026-10-01", "Progress": 1,
                  "Results": {"Statistics": stats}}
        self.assertEqual(parse_result(packet)["period"], {"start": "2020-01-01", "end": "2020-12-31"})
        self.assertEqual(parse_result({"Statistics": stats, "created": "2026-10-01"})["period"],
                         {"start": None, "end": None})

    def test_ambiguous_percentages_and_nonfinite_statistics_are_rejected(self):
        for value in (140.67, "140.67", "NaN%", "Infinity%"):
            data = fixture()
            data["backtest"]["statistics"]["Net Profit"] = value
            with self.assertRaises(ValueError):
                parse_result(data)

    def test_missing_statistic_stays_unknown_and_failed_api_is_not_a_backtest(self):
        data = fixture()
        del data["backtest"]["statistics"]["Compounding Annual Return"]
        self.assertIsNone(parse_result(data)["metrics"]["annual_return"])
        data["success"] = False
        with self.assertRaises(ValueError):
            parse_result(data)

    def test_conflicting_period_fields_and_wrong_metric_units_are_rejected(self):
        data = fixture()
        data["backtest"]["periodStart"] = "2021-01-01"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            parse_result(data)
        data = fixture()
        data["backtest"]["statistics"]["Sharpe Ratio"] = "1.25%"
        with self.assertRaisesRegex(ValueError, "units"):
            parse_result(data)


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.experiment = self.root / "baseline"
        export_backtest(ROOT / "portfolio_config.json", self.experiment)
        self.raw = self.root / "qc.json"
        self.raw.write_text(json.dumps(fixture()), encoding="utf-8")

    def archive(self, **kwargs):
        return import_result(self.experiment, self.raw, parse_result, **kwargs)

    def test_archives_raw_evidence_and_does_not_promote_the_export_to_success(self):
        before = (self.experiment / "manifest.json").read_bytes()
        result = self.archive()
        record = verified_record(result)
        self.assertEqual((result / "raw-result.json").read_bytes(), self.raw.read_bytes())
        self.assertEqual(record["code_evidence"], "source_unverified")
        self.assertEqual(before, (self.experiment / "manifest.json").read_bytes())
        with self.assertRaises(FileExistsError):
            self.archive()

    def test_code_copy_matches_despite_newline_style_but_edits_are_rejected(self):
        code = self.root / "run-main.py"
        code.write_bytes((self.experiment / "main.py").read_text().replace("\n", "\r\n").encode())
        result = self.archive(run_code=code)
        self.assertEqual(verified_record(result)["code_evidence"], "supplied_code_matches_export")
        code.write_text(code.read_text() + "\n# changed after running\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            self.archive(run_code=code)

    def test_changed_export_config_or_source_and_mismatched_period_are_rejected(self):
        manifest_path = self.experiment / "manifest.json"
        original = manifest_path.read_text()
        manifest = json.loads(original)
        manifest["configuration"]["strategy"]["count"] = 4
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "configuration"):
            self.archive()
        manifest_path.write_text(original)
        data = fixture()
        data["backtest"]["runtimeStatistics"]["Research end"] = "2021-12-31"
        self.raw.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "period"):
            self.archive()
        with (self.experiment / "main.py").open("a") as handle:
            handle.write("\n# changed\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.archive()

    def test_missing_dates_are_not_verified_from_configuration(self):
        data = fixture()
        data["backtest"]["runtimeStatistics"] = {}
        self.raw.write_text(json.dumps(data))
        record = verified_record(self.archive())
        self.assertIsNone(record["result"]["period"]["start"])
        self.assertTrue(any("dates missing" in w for w in record["warnings"]))

    def test_compare_identifies_cost_and_period_differences_without_a_winner(self):
        first = self.archive()
        second_exp = self.root / "stress"
        export_backtest(ROOT / "portfolio_config.json", second_exp, start="2022-01-01",
                        end="2023-12-31", slippage_bps=30, note="Costs and different period; synthetic fixture")
        data = fixture()
        data["backtest"]["runtimeStatistics"].update({"Research start": "2022-01-01", "Research end": "2023-12-31"})
        self.raw.write_text(json.dumps(data))
        second = import_result(second_exp, self.raw, parse_result)
        out = self.root / "compare"
        report = compare_results([first, second], parse_result, out)
        self.assertEqual(report["comparison_differences"], ["start", "end", "slippage_bps"])
        self.assertNotIn("winner", report)
        self.assertTrue((out / "comparison.md").is_file())
        with self.assertRaises(FileExistsError):
            compare_results([first, second], parse_result, out)

    def test_modified_raw_results_or_summary_metrics_cannot_be_compared(self):
        first = self.archive()
        second = self.archive(run_code=self.experiment / "main.py")
        raw = first / "raw-result.json"
        original = raw.read_bytes()
        raw.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            compare_results([first, second], parse_result, self.root / "a")
        raw.write_bytes(original)
        summary = first / "summary.json"
        data = read_json(summary)
        data["result"]["metrics"]["total_return"] = 999
        summary.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "summary"):
            compare_results([first, second], parse_result, self.root / "b")

    def test_nonstandard_json_and_duplicate_keys_are_rejected(self):
        for text in ('{"x":NaN}', '{"x":1,"x":2}'):
            self.raw.write_text(text)
            with self.assertRaises(ValueError):
                self.archive()


if __name__ == "__main__":
    unittest.main()
