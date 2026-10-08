"""Workspace export requests cannot silently export a different selected strategy."""
import json
import unittest
from unittest.mock import patch

from tests import test_portal as fixture
from app.control_contracts import ControlError
from app import workbench
from app.execution_policy import CURRENT_EXECUTION_POLICY


class ResearchStrategyBindingTests(unittest.TestCase):
    setUp = fixture.PortalTests.setUp
    tearDown = fixture.PortalTests.tearDown

    def request(self, expected):
        return {'start': '2020-01-01', 'end': '2020-12-31', 'slippage_bps': 10,
                'expected_strategy_type': expected}

    def create_instance(self):
        strategy = json.loads((fixture.ROOT / 'tests/fixtures/portfolio_default.json').read_text(encoding='utf-8'))['strategy']
        value = {'name': 'Research copy', 'strategy': strategy, 'budget': '.5',
                 'broker_id': self.service.broker_id, 'source_id': 'massive', 'schedule': {}}
        reply = self.client.post('/api/strategy-instances', json=value)
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply.json()['id'], value

    def assert_no_export(self):
        self.assertFalse((self.service.folder / 'experiments').exists())
        self.assertFalse((self.service.folder / 'instance-configs').exists())
        self.assertFalse(self.calls)

    def test_global_export_rejects_strategy_changed_in_another_tab(self):
        cfg, version = self.service.config()
        viewed = cfg['strategy']['type']
        updated = self.client.post('/api/strategy', json={'version': version,
            'strategy': {'type': 'equal_weight', 'count': 4, 'weight': .19}})
        self.assertEqual(updated.status_code, 200, updated.text)
        reply = self.client.post('/api/experiments', json=self.request(viewed))
        self.assertEqual(reply.status_code, 409, reply.text)
        self.assertIn('策略已变化', reply.json()['message'])
        self.assert_no_export()

    def test_instance_export_rejects_instance_changed_in_another_tab(self):
        ident, value = self.create_instance()
        viewed = value['strategy']['type']
        value['strategy'] = {'type': 'equal_weight', 'count': 4, 'weight': .19}
        updated = self.client.post('/api/strategy-instances/' + ident, json=value)
        self.assertEqual(updated.status_code, 200, updated.text)
        reply = self.client.post(f'/api/strategy-instances/{ident}/export', json=self.request(viewed))
        self.assertEqual(reply.status_code, 409, reply.text)
        self.assertIn('策略已变化', reply.json()['message'])
        self.assert_no_export()

    def test_both_export_routes_reject_explicit_invalid_expected_strategy(self):
        ident, _ = self.create_instance()
        for expected in (None, '', '  ', 42, True, [], {}):
            for path in ('/api/experiments', f'/api/strategy-instances/{ident}/export'):
                with self.subTest(expected=expected, path=path):
                    reply = self.client.post(path, json=self.request(expected))
                    self.assertEqual(reply.status_code, 409, reply.text)
                    self.assertIn('非空文本', reply.json()['message'])
        self.assert_no_export()

    def test_matched_and_legacy_requests_export_without_connecting_account(self):
        ident, value = self.create_instance()
        expected = value['strategy']['type']
        for path in ('/api/experiments', f'/api/strategy-instances/{ident}/export'):
            for include_expected in (True, False):
                body = self.request(expected)
                if not include_expected:
                    del body['expected_strategy_type']
                reply = self.client.post(path, json=body)
                self.assertEqual(reply.status_code, 200, reply.text)
                manifest = json.loads((self.service.folder / 'experiments' / reply.json()['id'] /
                                       'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(manifest['configuration']['strategy']['type'], expected)
        self.assertFalse(self.calls)

    def test_service_direct_call_rejects_wrong_strategy_before_export(self):
        with self.assertRaises(ControlError):
            self.service.export('2020-01-01', '2020-12-31', 10, expected_strategy_type='wrong')
        self.assert_no_export()

    def test_export_uses_checked_snapshot_even_if_config_file_changes_after_validation(self):
        selected = self.service.config()[0]['strategy']['type']
        real_export = workbench.export_backtest

        def change_local_file(config_path, out, **kwargs):
            path = self.root / 'configs/portfolio_config.json'
            config = json.loads(path.read_text(encoding='utf-8'))
            config['strategy'] = {'type': 'equal_weight', 'count': 4, 'weight': .19}
            path.write_text(json.dumps(config), encoding='utf-8')
            return real_export(config_path, out, **kwargs)

        with patch.object(workbench, 'export_backtest', side_effect=change_local_file):
            reply = self.client.post('/api/experiments', json=self.request(selected))
        self.assertEqual(reply.status_code, 200, reply.text)
        manifest = json.loads((self.service.folder / 'experiments' / reply.json()['id'] /
                               'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['configuration']['strategy']['type'], selected)
        self.assertFalse(list(self.service.folder.glob('export-config-*.json')))
        self.assertFalse(self.calls)

    def test_state_reports_deployment_scope_separately_from_selected_account(self):
        state = self.client.get('/api/state').json()
        self.assertEqual(state['execution_scope'], CURRENT_EXECUTION_POLICY.public())
        self.assertEqual(state['mode'], state['broker']['mode'])
        self.assertIn('live', state['execution_scope']['known_environments'])
        self.assertNotIn('live', state['execution_scope']['allowed_environments'])


if __name__ == '__main__':
    unittest.main()
