"""Execution scope is deployment policy, not a promise of connector readiness."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.control_contracts import ControlError
from app.execution_policy import ACCOUNT_ENVIRONMENTS, CURRENT_EXECUTION_POLICY
from app.module_registry import ModuleRegistry
from app import portal_launcher
from portal.app import create_app
from portal.controller import create_controller


class ExecutionPolicyTests(unittest.TestCase):
    def test_live_is_known_but_not_authorized_and_public_output_cannot_change_policy(self):
        policy = CURRENT_EXECUTION_POLICY
        self.assertIn('live', ACCOUNT_ENVIRONMENTS)
        self.assertTrue(policy.allows('paper'))
        self.assertTrue(policy.allows('broker_paper'))
        self.assertFalse(policy.allows('live'))
        for mode in ('live', 'LIVE', 'unknown', None, ['paper']):
            with self.assertRaises(ControlError):
                policy.require(mode)
        public = policy.public()
        public['allowed_environments'].append('live')
        public['live_enabled'] = True
        self.assertFalse(policy.allows('live'))
        with self.assertRaises(FrozenInstanceError):
            policy.identifier = 'live'
        with self.assertRaises(TypeError):
            ACCOUNT_ENVIRONMENTS['live'] = 'allowed'

    def test_registry_blocks_disallowed_modes_before_loading_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'configs').mkdir()
            (root / 'configs/portfolio_config.json').write_text('{}', encoding='utf-8')
            registry = ModuleRegistry(root)
            for mode in ('live', 'unregistered'):
                entry = {'type': 'example', 'version': '1', 'control': {
                    'mode': mode, 'entrypoint': 'brokers.missing_live_sdk:Example',
                    'capabilities': ['inspect', 'execute']}}
                with patch.object(registry, 'resolve', return_value=({'type': 'example'}, entry)), \
                        patch('app.module_registry.load_class') as load, \
                        patch('app.module_registry.normalize_config', return_value={}):
                    descriptor = registry.descriptor('broker', 'example')
                    self.assertEqual(descriptor['mode'], mode)
                    self.assertFalse(descriptor['enabled'])
                    self.assertIn('当前部署', descriptor['reason'])
                    for operation in (lambda: registry.control('broker', 'example'),
                                      lambda: registry.configuration('example')):
                        with self.assertRaises(ControlError):
                            operation()
                    load.assert_not_called()

    def test_health_and_launcher_report_one_policy_without_opening_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service, runtime = Mock(), Mock()
            apps = [create_app(root, service=service),
                    create_controller(root, port=8765, instance='test', runtime=runtime)]
            for app in apps:
                client = TestClient(app, base_url='http://127.0.0.1:8765')
                try:
                    report = client.get('/health').json()
                    self.assertEqual(report['execution_policy'], 'simulation_only')
                    self.assertEqual(report['execution_scope'], CURRENT_EXECUTION_POLICY.public())
                finally:
                    client.close()
            service.assert_not_called()
            self.assertEqual(service.mock_calls, [])
            self.assertEqual(runtime.mock_calls, [])
            target = root / 'self-test.json'
            with patch('sys.argv', ['launcher', '--self-test', str(target)]), \
                    patch.object(portal_launcher, 'project_root', return_value=root), \
                    patch.object(portal_launcher, 'start', return_value='http://127.0.0.1:8765'), \
                    patch.object(portal_launcher.webbrowser, 'open') as browser:
                self.assertEqual(portal_launcher.main(), 0)
            report = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(report['execution_policy'], CURRENT_EXECUTION_POLICY.identifier)
            self.assertEqual(report['execution_scope'], CURRENT_EXECUTION_POLICY.public())
            self.assertFalse(report['broker_requested'])
            browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
