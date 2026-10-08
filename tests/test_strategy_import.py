"""Import an actual strategy bundle through the owner API and run isolated selection."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
import strategies
from app.module_registry import ModuleRegistry
from app.strategy_worker import selection
from app.workbench import PortalService
from portal.app import create_app
from test_tech_profit_momentum import stock

ROOT = Path(__file__).resolve().parents[1]


class StrategyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('app', 'configs', 'strategies', 'framework', 'brokers', 'data', 'platforms'):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns('__pycache__'))
        # The general regression harness installs legacy test fixtures in its
        # disposable workspace. This integration test uses the shipping catalog.
        for name in ('equal_weight', 'hold', 'ma', 'momentum'):
            (self.root / 'strategies/plugins' / (name + '.json')).unlink(missing_ok=True)
        shutil.copyfile(ROOT / 'tests/fixtures/portfolio_default.json', self.root / 'configs/portfolio_config.json')
        shutil.copyfile(ROOT / 'tests/fixtures/broker_profiles.json', self.root / 'configs/broker_profiles.json')
        self.assembly = patch('app.assembly.ROOT', self.root)
        self.assembly.start()
        self.addCleanup(self.assembly.stop)
        self.namespace = patch.object(strategies, '__path__', [str(self.root / 'strategies')])
        self.namespace.start()
        self.addCleanup(self.namespace.stop)
        self.service = PortalService(self.root)
        self.client = TestClient(create_app(self.root, service=self.service), base_url='http://127.0.0.1:8765')
        self.addCleanup(self.client.close)
        self.token = self.client.get('/api/session').json()['token']
        self.client.headers['X-Portal-Token'] = self.token

    def bundle(self, ident='imported_tech'):
        response = self.client.get('/api/strategy-plugins/tech_profit_momentum/bundle')
        self.assertEqual(response.status_code, 200)
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(response.content)) as original, zipfile.ZipFile(output, 'w') as package:
            self.assertEqual(set(original.namelist()), {'strategy.py', 'manifest.json', 'README.md'})
            manifest = json.loads(original.read('manifest.json'))
            manifest.update(type=ident, display_name='Imported technology momentum')
            package.writestr('manifest.json', json.dumps(manifest))
            package.writestr('strategy.py', original.read('strategy.py'))
            package.writestr('README.md', original.read('README.md'))
        return output.getvalue()

    def test_api_import_preserves_readme_and_runs_same_isolated_selection(self):
        config_path = self.root / 'configs/portfolio_config.json'
        config_before = config_path.read_bytes()
        databases_before = {p.relative_to(self.root) for p in (self.root / 'runs').rglob('*.sqlite')}
        self.assertEqual(set(self.service.registry.catalog('strategy')), {'mom8', 'tech_profit_momentum'})
        archive = self.bundle()
        result = self.client.post('/api/strategy-plugins/upload', content=archive)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['type'], 'imported_tech')
        reloaded = ModuleRegistry(self.root)
        catalog = {s['type']: s for s in reloaded.strategies()}
        imported = catalog['imported_tech']
        self.assertTrue(imported['enabled'])
        self.assertEqual(imported['readme']['content'], catalog['tech_profit_momentum']['readme']['content'])
        self.assertEqual(imported['defaults'], catalog['tech_profit_momentum']['defaults'])
        strategy = dict(type='imported_tech', **imported['defaults'])
        created = self.client.post('/api/strategy-instances', json=dict(
            name='Imported strategy check', strategy=strategy, budget='.5',
            broker_id=reloaded.default('broker'), source_id='massive', schedule={}))
        self.assertEqual(created.status_code, 200, created.text)
        item = self.service.strategy_instances.get(created.json()['id'])
        self.assertFalse(item['enabled'])
        limits = SimpleNamespace(max_gross=Decimal('.95'), max_weight=Decimal('.2'))
        stocks = [stock('A', .1), stock('B', .5), stock('C', .2, industry='hardware')]
        expected_item = deepcopy(item)
        expected_item['strategy']['type'] = 'tech_profit_momentum'
        expected = selection(self.root, expected_item, stocks, date(2020, 1, 2), (), limits, 10)
        actual = selection(self.root, item, stocks, date(2020, 1, 2), (), limits, 10)
        self.assertTrue(actual)
        self.assertEqual(actual, expected)
        self.assertEqual(config_before, config_path.read_bytes())
        self.assertFalse(self.service.automation.data['enabled'])
        self.assertEqual({p.relative_to(self.root) for p in (self.root / 'runs').rglob('*.sqlite')}, databases_before)
        self.assertEqual(self.client.post('/api/strategy-plugins/upload', content=archive).status_code, 409)
        with zipfile.ZipFile(io.BytesIO(self.client.get('/api/strategy-plugins/imported_tech/bundle').content)) as exported:
            self.assertEqual(set(exported.namelist()), {'manifest.json', 'strategy.py', 'README.md'})

    def test_import_requires_session_and_rejects_loose_python_or_nested_zip(self):
        archive = self.bundle()
        result = self.client.post('/api/strategy-plugins/upload', content=archive, headers={'X-Portal-Token': ''})
        self.assertEqual(result.status_code, 403)
        for invalid in (b'class MyStrategy: pass', b'not-a-zip'):
            self.assertEqual(self.client.post('/api/strategy-plugins/upload', content=invalid).status_code, 409)
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as package:
            package.writestr('folder/strategy.py', 'class MyStrategy: pass')
            package.writestr('folder/manifest.json', '{}')
        self.assertEqual(self.client.post('/api/strategy-plugins/upload', content=nested.getvalue()).status_code, 409)
        self.assertEqual(set(self.service.registry.catalog('strategy')), {'mom8', 'tech_profit_momentum'})


if __name__ == '__main__':
    unittest.main()
