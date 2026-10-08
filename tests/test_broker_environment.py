"""Live controls use the same workflow while grants and account identities stay isolated."""
from contextlib import contextmanager
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.assembly import broker_catalog, open_account_session
from app.control_contracts import ControlError
from app.module_registry import ModuleRegistry
from brokers.controls import OpenDLiveControl, OpenDPaperControl, ExecutionAuthorizedBroker
from framework.trading import Fees
from platforms.opend import OpenDError
import test_opend_live as live_fixture_module


class BrokerEnvironmentTests(unittest.TestCase):
    def test_live_and_paper_manifests_have_distinct_controls_and_same_declared_market(self):
        catalog = broker_catalog()
        for provider in ('futu', 'moomoo'):
            live, paper = (catalog[provider + suffix]['control'] for suffix in ('_live', '_paper'))
            self.assertEqual(live['entrypoint'], 'brokers.controls:OpenDLiveControl')
            self.assertEqual(paper['entrypoint'], 'brokers.controls:OpenDPaperControl')
            self.assertEqual(live['execution_context'], paper['execution_context'])
            self.assertIn('observe', live['capabilities'])
            self.assertIn('reconcile', live['capabilities'])

    def test_account_session_rejects_missing_grant_before_state_creation(self):
        connector = Mock(mode='live')
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'uncreated'
            connector.state_dir = target
            with patch('app.assembly._build_platform', return_value=connector):
                for changes in ({}, {'enable_live': True}, {'expected_mode': 'broker_paper'}):
                    args = dict(expected_mode='live', **{})
                    args.update(changes)
                    with self.assertRaises(ValueError):
                        with open_account_session({'broker': {}}, None, target, Fees(), **args):
                            self.fail('opened unapproved account')
            self.assertFalse(target.exists())
            connector.initialize.assert_not_called()
            connector.connect.assert_not_called()

    def live_fixture(self):
        fixture = live_fixture_module.FutuLiveTests('test_live_reads_and_writes_use_real_scope_and_do_not_auto_unlock')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_live_readonly_reconciliation_neither_unlocks_nor_allows_submit_cancel(self):
        fixture = self.live_fixture()
        fixture.trade._allow_orders = False
        connection = fixture.connector.connect(fixture.folder, None, Fees(), read_only=True)
        self.addCleanup(connection.close)
        self.assertEqual(connection.mode, 'live')
        self.assertFalse(connection.live_enabled)
        self.assertEqual(connection.query('unknown').status, 'unknown')
        for action in (lambda: connection.submit(fixture.intent), lambda: connection.cancel('1'),
                       lambda: connection.unlock('not-a-real-password')):
            with self.assertRaises(OpenDError):
                action()
        fixture.trade.place_order.assert_not_called()
        fixture.trade.modify_order.assert_not_called()
        fixture.trade.unlock_trade.assert_not_called()

    def test_actual_connected_identity_must_match_grant_before_returning_writer(self):
        fixture = self.live_fixture()
        with self.assertRaisesRegex(OpenDError, 'server execution grant'):
            fixture.connector.connect(fixture.folder, None, Fees(), enable_live=True,
                                      expected_account_ref='b' * 64)
        fixture.trade.place_order.assert_not_called()
        fixture.trade.unlock_trade.assert_not_called()

    def test_live_batch_rechecks_revocation_and_binding_before_each_write(self):
        from app.execution_policy import load_execution_policy
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'configs').mkdir()
            path = root / 'configs/execution_policy.json'
            path.write_text(json.dumps({'version':1, 'live_accounts': {'account': {'account_ref':'a'*64}}}))
            target = Mock()
            bound = ['a' * 64]
            def revalidate():
                load_execution_policy(root).require_execution('live', 'account', bound[0])
            broker = ExecutionAuthorizedBroker(target, revalidate)
            broker.submit('first')
            target.submit.assert_called_once_with('first')
            path.write_text(json.dumps({'version':1, 'live_accounts': {}}))
            for action in (lambda: broker.submit('second'), lambda: broker.cancel('order'),
                           lambda: broker.unlock('test-only')):
                with self.assertRaises(ControlError):
                    action()
            broker.query('order')
            target.query.assert_called_once_with('order')
            target.cancel.assert_not_called()
            target.unlock.assert_not_called()
            target.submit.assert_called_once()

    def test_missing_or_malformed_execution_context_cannot_fall_back_to_us(self):
        from brokers.controls import BrokerControl
        for context in (None, {}, {'market':'HK'}, {'market':'HK','timezone':'bad', 'currency':'HKD',
                         'asset_class':'equity','instrument_rules': {'quantity_step':'1','min_quantity':'1',
                         'price_tick':'.01','contract_multiplier':'1'}}):
            entry = {'control': {'mode':'broker_paper', 'execution_context':context}}
            control = BrokerControl(Path.cwd(), {'state_dir':'runs/test'}, entry)
            with self.assertRaises(ControlError):
                control.execution_context()

    def test_cli_cannot_bypass_registered_profile_authorization(self):
        from app.assembly import registered_live_control
        root = Path.cwd()
        control = Mock()
        control.connector_config.return_value = {'type':'example', 'state_dir':str(root / 'runs/example')}
        control.require_execution.side_effect = ControlError('no account grant')
        registry = Mock()
        registry.profiles.return_value = {'only':{}}
        registry.resolve.return_value = ({'type':'example'}, {'control':{'mode':'live','path_fields':['state_dir']}})
        registry.control.return_value = control
        with patch('app.assembly.ROOT', root), patch('app.module_registry.ModuleRegistry', return_value=registry):
            with self.assertRaisesRegex(ControlError, 'no account grant'):
                registered_live_control({'broker': {'type':'example','state_dir':'runs/example'}})
            control.require_execution.assert_called_once()
            registry.profiles.return_value = {'one':{},'two':{}}
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                registered_live_control({'broker': {'type':'example','state_dir':'runs/example'}})

    def test_registry_context_is_owned_by_new_module_not_broker_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = ModuleRegistry(root)
            context = dict(market='HK', timezone='Asia/Hong_Kong', currency='HKD', asset_class='equity',
                           instrument_rules=dict(quantity_step='100', min_quantity='100', price_tick='0.05', contract_multiplier='1'))
            profile = {'type': 'other_vendor', 'state_dir': 'runs/other-vendor'}
            entry = {'type': 'other_vendor', 'version': '1', 'control': {'mode': 'broker_paper',
                'entrypoint': 'brokers.controls:BrokerControl', 'capabilities': ['inspect', 'execute'],
                'execution_context': context}}
            with patch.object(registry, 'resolve', return_value=(profile, entry)):
                descriptor = registry.descriptor('broker', 'other_vendor')
                self.assertTrue(descriptor['enabled'])
                self.assertTrue(descriptor['execution_enabled'])
                self.assertEqual(descriptor['execution_context'], context)


if __name__ == '__main__':
    unittest.main()
