"""Execution scope is deployment policy, not a promise of connector readiness."""
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.control_contracts import ControlError
from app.execution_policy import ACCOUNT_ENVIRONMENTS, CURRENT_EXECUTION_POLICY, load_execution_policy
from app.module_registry import ModuleRegistry
from app import portal_launcher
from portal.app import create_app
from portal.controller import create_controller


class ExecutionPolicyTests(unittest.TestCase):
    def test_environment_support_is_separate_from_account_execution_grants(self):
        policy = CURRENT_EXECUTION_POLICY
        for mode in ACCOUNT_ENVIRONMENTS:
            self.assertTrue(policy.allows(mode))
        for mode in ('LIVE', 'unknown', None, ['paper']):
            with self.assertRaises(ControlError):
                policy.require(mode)
        with self.assertRaises(ControlError):
            policy.require_execution('live', 'example', 'a' * 64)
        public = policy.public()
        public['live_enabled'] = True
        public['live_profiles'].append('example')
        with self.assertRaises(ControlError):
            policy.require_execution('live', 'example', 'a' * 64)
        with self.assertRaises(FrozenInstanceError):
            policy.identifier = 'live'
        with self.assertRaises(TypeError):
            ACCOUNT_ENVIRONMENTS['live'] = 'allowed'

    def test_server_grant_is_pinned_to_profile_and_account_and_revocable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'configs').mkdir()
            path = root / 'configs/execution_policy.json'
            self.assertFalse(load_execution_policy(root).public()['live_enabled'])
            path.write_text(json.dumps({'version': 1, 'live_accounts': {
                'example': {'account_ref': 'a' * 64}}}), encoding='utf-8')
            policy = load_execution_policy(root)
            policy.require_execution('live', 'example', 'a' * 64)
            for profile, account in [('other', 'a' * 64), ('example', 'b' * 64), ('example', None)]:
                with self.assertRaises(ControlError):
                    policy.require_execution('live', profile, account)
            path.write_text(json.dumps({'version': 1, 'live_accounts': {}}), encoding='utf-8')
            with self.assertRaises(ControlError):
                load_execution_policy(root).require_execution('live', 'example', 'a' * 64)
            for value in ({'version': 1, 'live_accounts': {'example': True}},
                          {'version': 1, 'live_accounts': {'example': {'account_ref': '*'}}},
                          {'version': True, 'live_accounts': {}}):
                path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(ControlError):
                    load_execution_policy(root)

    def test_registry_can_load_live_readonly_control_without_authorizing_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = ModuleRegistry(root)
            entry = {'type': 'example', 'version': '1', 'control': {
                'mode': 'live', 'entrypoint': 'brokers.controls:OpenDLiveControl',
                'capabilities': ['inspect', 'execute'],
                'execution_context': {'market':'US','timezone':'America/New_York','currency':'USD',
                    'asset_class':'equity','instrument_rules': {'quantity_step':'1','min_quantity':'1',
                    'price_tick':'.001','contract_multiplier':'1'}}}}
            profile = {'type': 'example', 'state_dir': 'runs/example'}
            with patch.object(registry, 'resolve', return_value=(profile, entry)), \
                    patch('brokers.controls.OpenDLiveControl.execution_account_reference', return_value='a' * 64):
                descriptor = registry.descriptor('broker', 'example')
                self.assertTrue(descriptor['enabled'])
                self.assertFalse(descriptor['execution_enabled'])
                self.assertIn('服务器执行授权', descriptor['execution_reason'])
                self.assertEqual(registry.control('broker', 'example').mode, 'live')
                with self.assertRaises(ControlError):
                    registry.require_execution('example')

    def test_registry_rejects_unknown_environment_before_import(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = ModuleRegistry(folder)
            entry = {'type': 'example', 'version': '1', 'control': {
                'mode': 'unregistered', 'entrypoint': 'brokers.missing_sdk:Example'}}
            with patch.object(registry, 'resolve', return_value=({'type': 'example'}, entry)), \
                    patch('app.module_registry.load_class') as load:
                self.assertFalse(registry.descriptor('broker', 'example')['enabled'])
                with self.assertRaises(ControlError):
                    registry.control('broker', 'example')
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
                    self.assertEqual(report['execution_policy'], 'account_scoped')
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
