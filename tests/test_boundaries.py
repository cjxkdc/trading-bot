import ast
import math
import unittest
from datetime import date, timedelta
from pathlib import Path
from framework.backtest import backtest


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.rows = tuple((date(2020, 1, 1) + timedelta(days=i), 10.0) for i in range(5))

    def run_strategy(self, strategy):
        return backtest(self.rows, strategy, ["2020-01-02", "2020-01-05"], 1000, 1, 100, 2)

    def test_new_strategy_and_memory_provider_need_no_engine_changes(self):
        class MemoryProvider:
            def load(inner):
                return self.rows

        class CustomStrategy:
            warmup = 1

            def target(inner, history):
                # The engine must not provide current execution-day/future bars.
                self.assertIsInstance(history, tuple)
                self.assertLess(history[-1][0], date(2020, 1, 5))
                return 0.5

        result = backtest(MemoryProvider().load(), CustomStrategy(),
                          ["2020-01-02", "2020-01-05"], 1000, 1, 0, 2)
        self.assertEqual(result["final_equity"], 1000)
        self.assertEqual(result["trades"], 1)

    def test_rejects_invalid_strategy_exposures(self):
        for value in (-0.1, 1.1, math.nan, math.inf):
            class Invalid:
                warmup = 1

                def target(self, history):
                    return value
            with self.assertRaises(ValueError):
                self.run_strategy(Invalid())

    def test_partial_reduction_cost_is_self_financing(self):
        class Reducing:
            warmup = 1

            def target(self, history):
                return 1.0 if len(history) == 1 else 0.5

        result = self.run_strategy(Reducing())
        after_buy = 1000 / 1.01
        sale = after_buy * 0.5 / 0.995
        self.assertAlmostEqual(result["final_equity"], after_buy - sale * 0.01)
        self.assertEqual(result["trades"], 2)

    def test_framework_has_no_concrete_provider_or_strategy_imports(self):
        root = Path(__file__).resolve().parents[1] / "framework"
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                self.assertFalse(any(n.split('.')[0] in {"strategies", "platforms", "app", "moomoo", "futu"} for n in names))
