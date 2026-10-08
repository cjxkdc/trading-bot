"""Connection edits cannot change account scope, leak identities, or strand orders."""
import json
import unittest
from unittest.mock import patch

from app.module_registry import ModuleRegistry
from app.control_contracts import ControlError
import test_portal


class BrokerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_portal.PortalTests('test_opening_portal_does_not_connect_or_create_a_trading_ledger')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service, self.client, self.root = self.fixture.service, self.fixture.client, self.fixture.root
        self.service.select_broker('moomoo_paper')
        self.path = self.root / 'secrets/moomoo/connection.json'

    def payload(self, **values):
        descriptor = self.service.registry.descriptor('broker', 'moomoo_paper')
        return dict(broker='moomoo_paper', revision=descriptor['settings_revision'], values=values)

    def save(self, **values):
        return self.client.post('/api/broker/settings', json=self.payload(**values))

    def seed(self):
        result = self.save(host='127.0.0.1', port=11112, account_id='123456789', login_user_id='987654321')
        self.assertEqual(result.status_code, 200, result.text)

    def test_save_is_offline_retains_blank_ids_and_redacts_state(self):
        self.seed()
        self.service.account = {'status': 'connected'}
        result = self.save(port=11113, account_id='', login_user_id='')
        self.assertEqual(result.status_code, 200, result.text)
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored['account_id'], '123456789')
        self.assertEqual(stored['port'], 11113)
        self.assertIsNone(self.service.account)
        self.assertEqual(self.fixture.calls, [])
        state = self.client.get('/api/state')
        self.assertEqual(state.status_code, 200)
        self.assertNotIn('123456789', state.text)
        self.assertNotIn('987654321', state.text)
        broker = state.json()['broker']
        self.assertEqual(broker['settings']['values']['port'], 11113)
        self.assertIn('account_id', broker['settings']['configured_fields'])
        self.assertEqual(result.json()['settings_revision'], broker['settings_revision'])
        self.assertFalse((self.root / 'runs/moomoo-paper/journal.sqlite').exists())

    def test_field_and_scope_validation_precedes_writes(self):
        for values in ({'port': True}, {'port': 70000}, {'host': 'example.com'},
                       {'account_id': None}, {'account_id': '-2'}, {'account_id': '1' * 33},
                       {'environment': 'REAL'}, {'market': 'HK'}, {'state_dir': 'runs/other'}):
            with self.subTest(values=values):
                self.assertEqual(self.save(**values).status_code, 409)
                self.assertFalse(self.path.exists())

    def test_stale_or_wrong_selected_profile_is_rejected(self):
        value = self.payload(port=11113)
        self.seed()
        saved = self.path.read_bytes()
        self.assertEqual(self.client.post('/api/broker/settings', json=value).status_code, 409)
        value = self.payload(port=11114)
        self.service.select_broker('futu_paper')
        self.assertEqual(self.client.post('/api/broker/settings', json=value).status_code, 409)
        self.assertEqual(self.path.read_bytes(), saved)

    def test_running_automation_or_unresolved_journal_blocks_configuration(self):
        self.seed()
        before = self.path.read_bytes()
        self.service.automation.data['enabled'] = True
        self.assertEqual(self.save(port=11113).status_code, 409)
        self.service.automation.data['enabled'] = False
        with patch('app.broker_settings.JournalReader.has_unresolved', return_value=True):
            self.assertEqual(self.save(port=11113).status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_account_ledger_prevents_identity_rebinding(self):
        self.seed()
        folder = self.root / 'runs/moomoo-paper'
        folder.mkdir(parents=True)
        (folder / 'moomoo-paper.sqlite').write_bytes(b'existing account state')
        before = self.path.read_bytes()
        self.assertEqual(self.save(account_id='333').status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.save(port=11113).status_code, 200)

    def test_live_settings_do_not_change_paper_or_grant_execution(self):
        self.seed()
        before = self.path.read_bytes()
        self.service.select_broker('moomoo_live')
        descriptor = self.service.registry.descriptor('broker', 'moomoo_live')
        response = self.client.post('/api/broker/settings', json=dict(broker='moomoo_live',
            revision=descriptor['settings_revision'], values=dict(port=11112, account_id='555', login_user_id='666')))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.service.registry.descriptor('broker', 'moomoo_live')['execution_enabled'])
        self.assertFalse((self.root / 'configs/execution_policy.json').exists())

    def test_plugin_declaring_configure_requires_both_contract_methods(self):
        registry = ModuleRegistry(self.root)
        profile, entry = registry.resolve('broker', 'paper')
        entry['control']['capabilities'].append('configure')
        with patch.object(registry, 'resolve', return_value=(profile, entry)):
            with self.assertRaises(ControlError):
                registry.descriptor('broker', 'paper')

    def test_malformed_existing_configuration_is_not_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{invalid')
        response = self.save(port=11113)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.path.read_text(), '{invalid')


if __name__ == '__main__':
    unittest.main()
