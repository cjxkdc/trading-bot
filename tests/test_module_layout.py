"""The five maintained modules and legacy entry points must agree."""
import ast
import importlib
from pathlib import Path
import unittest
from app.assembly import data_catalog, broker_catalog, platform_catalog

ROOT = Path(__file__).resolve().parents[1]


class ModuleLayoutTests(unittest.TestCase):
    def test_catalogs_are_separate_and_legacy_catalog_is_their_union(self):
        data, brokers = data_catalog(), broker_catalog()
        self.assertFalse(data.keys() & brokers.keys())
        self.assertEqual(platform_catalog(), dict(data, **brokers))
        for entry in data.values(): self.assertTrue(entry['entrypoint'].startswith('data.'))
        for entry in brokers.values(): self.assertTrue(entry['entrypoint'].startswith('brokers.'))
        self.assertIn('massive_technology', data)

    def test_compatibility_imports_are_same_module_not_duplicate_state(self):
        for old, new in [('platforms.brokers.paper', 'brokers.paper'),
                         ('platforms.brokers.tiger_paper', 'brokers.tiger_paper'),
                         ('platforms.data.snapshot', 'data.snapshot'),
                         ('platforms.data.futu_technology', 'data.futu_technology'),
                         ('framework.backtest', 'backtests.engine'),
                         ('framework.experiments', 'backtests.experiments')]:
            self.assertIs(importlib.import_module(old), importlib.import_module(new))

    def test_data_does_not_depend_on_brokers_strategies_or_assembly(self):
        for path in (ROOT / 'data').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                names = [v.name for v in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                self.assertFalse(any(n.split('.')[0] in {'brokers', 'strategies', 'assembly'}
                                     or n.startswith(('platforms.brokers', 'app.assembly')) for n in names), path.name)

    def test_framework_and_strategies_never_import_concrete_data_or_broker_modules(self):
        for folder in ('framework', 'strategies'):
            for path in (ROOT / folder).glob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names = [v.name for v in node.names] if isinstance(node, ast.Import) else (
                        [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                    self.assertFalse(any(n.split('.')[0] in {'data', 'brokers'} for n in names), path.name)
