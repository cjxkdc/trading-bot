"""Plug-in failure isolation, shared operations and order-only execution boundaries."""
import ast
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from app.control_contracts import ControlError
from app.readiness import operation_states
from app.workbench_cli import WorkbenchClient
from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS, order_capability_issues
from tests import test_portal as fixture
from tests import test_paper_preparation as paper_fixture


class PluginIsolationTests(unittest.TestCase):
    setUp = fixture.PortalTests.setUp
    tearDown = fixture.PortalTests.tearDown
    finish = fixture.PortalTests.finish

    def write(self, folder, name, entry):
        path = self.root / folder / 'plugins' / (name + '.json')
        path.write_text(json.dumps(entry), encoding='utf-8')
        return path

    def test_unused_strategy_missing_dependency_is_disabled_and_state_remains_available(self):
        self.write('strategies', 'optional_broken', {'type':'optional_broken','kind':'portfolio',
            'version':'1','entrypoint':'strategies.missing_optional_dependency:Example'})
        reply = self.client.get('/api/state')
        self.assertEqual(reply.status_code, 200)
        state = reply.json()
        self.assertFalse(next(s for s in state['strategies'] if s['type']=='optional_broken')['enabled'])
        self.assertTrue(state['actions']['status']['enabled'])
        self.assertEqual(self.calls, [])

    def test_unused_malformed_manifest_and_broker_profile_do_not_hide_valid_plugins(self):
        (self.root/'data/plugins/broken.json').write_text('{', encoding='utf-8')
        path = self.root/'configs/broker_profiles.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['profiles']['broken_account'] = {'config_file':'configs/missing.json'}
        path.write_text(json.dumps(value), encoding='utf-8')
        state = self.client.get('/api/state').json()
        self.assertTrue(any(s.get('file')=='broken.json' and not s['enabled'] for s in state['sources']))
        self.assertFalse(next(b for b in state['brokers'] if b['id']=='broken_account')['enabled'])
        self.assertTrue(state['actions']['status']['enabled'])

    def test_duplicate_name_disables_both_registrations_instead_of_selecting_one(self):
        existing = json.loads((self.root/'strategies/plugins/mom8.json').read_text(encoding='utf-8'))
        self.write('strategies','duplicate_mom8',existing)
        catalog = self.service.registry.catalog('strategy')
        self.assertNotIn('mom8', catalog)
        self.assertEqual(len(catalog.issues), 2)
        self.assertIn('tech_profit_momentum', catalog)

    def test_invalid_settings_metadata_cannot_break_page_serialization(self):
        entry=json.loads((self.root/'data/plugins/massive.json').read_text(encoding='utf-8'))
        entry['type']='bad_settings'
        entry['control']['settings_fields']=[42]
        self.write('data','bad_settings',entry)
        state=self.client.get('/api/state').json()
        self.assertTrue(state['actions']['status']['enabled'])
        self.assertTrue(any(s.get('file')=='bad_settings.json' and not s['enabled'] for s in state['sources']))

    def test_declared_but_unimplemented_capability_disables_only_that_plugin(self):
        entry=json.loads((self.root/'data/plugins/snapshot_file.json').read_text(encoding='utf-8'))
        entry['type']='bad_collector'
        entry['control']['capabilities'].append('collect')
        self.write('data','bad_collector',entry)
        state=self.client.get('/api/state').json()
        bad=next(s for s in state['sources'] if s['id']=='bad_collector')
        self.assertFalse(bad['enabled'])
        self.assertTrue(state['actions']['status']['enabled'])

    def test_active_broken_strategy_blocks_trade_but_allows_account_check(self):
        name = self.service.config()[0]['strategy']['type']
        path = self.root/'strategies/plugins'/f'{name}.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        value['entrypoint'] = 'strategies.missing_active_dependency:Example'
        path.write_text(json.dumps(value), encoding='utf-8')
        state = self.client.get('/api/state').json()
        self.assertFalse(state['actions']['execute']['enabled'])
        self.assertTrue(state['actions']['status']['enabled'])
        self.assertEqual(self.client.post('/api/jobs',json={'action':'prepare'}).status_code,409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post('/api/jobs',json={'action':'offline'}).status_code,200)
        self.finish()
        self.assertEqual(len(self.calls),1)

    def test_missing_selected_data_does_not_block_broker_operation(self):
        self.service.source='deleted_data_source'
        state=self.service.state()
        self.assertFalse(state['actions']['preview']['enabled'])
        self.assertTrue(state['actions']['status']['enabled'])
        self.service.start('status')
        self.finish()
        self.assertEqual(self.calls[0][0][1],'status')

    def test_ui_reason_and_post_rejection_agree_and_worker_does_not_run(self):
        state=self.service.state()
        reason=state['actions']['execute']['message']
        response=self.client.post('/api/jobs',json={'action':'execute','confirmation':self.service.broker_id})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['message'],reason)
        self.assertEqual(self.calls,[])


class ReadinessTests(unittest.TestCase):
    def states(self, **changes):
        args=dict(broker={'enabled':True,'mode':'broker_paper','capabilities':['inspect','prepare','execute']},
            source={'enabled':True,'configured':True,'capabilities':['preview','collect','inspect','configure','paper_signal']},
            snapshot={'research_only':True},preview={'execution_ready':True,'expires_at':200},
            verified=True,account={'status':'connected'},preview_authorized=True,clock=100)
        args.update(changes)
        return operation_states(**args)

    def test_expired_account_verification_has_explicit_reason_and_next_action(self):
        result=self.states(verified=False)['execute']
        self.assertFalse(result['enabled'])
        self.assertEqual(result['blockers'][0]['code'],'account_unverified')
        self.assertEqual(result['next_action'],'status')

    def test_stale_quotes_disable_execution_without_disabling_reprepare(self):
        states=self.states(clock=201)
        self.assertFalse(states['execute']['enabled'])
        self.assertEqual(states['execute']['next_action'],'prepare')
        self.assertTrue(states['prepare']['enabled'])

    def test_missing_private_approval_and_research_preview_cannot_enable_execute(self):
        self.assertFalse(self.states(preview_authorized=False)['execute']['enabled'])
        self.assertFalse(self.states(preview={'execution_ready':False})['execute']['enabled'])

    def test_broken_strategy_does_not_disable_configuration_or_account_diagnostics(self):
        states=self.states(strategy_issue='broken',context_issue='unavailable')
        self.assertTrue(states['status']['enabled'])
        self.assertTrue(states['configure-data']['enabled'])
        self.assertFalse(states['prepare']['enabled'])


class OrderCapabilityTests(unittest.TestCase):
    setUp=paper_fixture.TestPreparation.setUp
    preparation=paper_fixture.TestPreparation.preparation

    def test_new_research_schema_can_prepare_same_orders_without_broker_change(self):
        original=deepcopy(self.control.entry)
        self.loaded=replace(self.loaded,schema='different_stock_research_v2')
        self.runner.input_schema=self.loaded.schema
        result=self.preparation()
        self.assertTrue(result['execution_ready'])
        self.assertEqual(self.control.entry,original)

    def test_unsupported_market_and_fractional_requirements_stop_before_account_access(self):
        self.control.observe_account=Mock(side_effect=AssertionError('must not connect'))
        for field,value in [('market','HK'),('quantity_type','fractional'),('asset_class','option')]:
            self.loaded=replace(self.loaded,order_requirements=replace(US_EQUITY_LIMIT_ORDERS,**{field:value}))
            with self.assertRaises(ControlError):
                self.preparation()
        self.control.observe_account.assert_not_called()

    def test_missing_capability_declaration_is_not_inferred_from_strategy_name(self):
        self.assertTrue(order_capability_issues(None,US_EQUITY_LIMIT_ORDERS))
        self.assertTrue(order_capability_issues({'supported_schemas':['us_equity_momentum_daily_v1']},US_EQUITY_LIMIT_ORDERS))


class CliBoundaryTests(unittest.TestCase):
    def client(self, ready=True):
        client=WorkbenchClient(url='http://127.0.0.1:12345')
        self.state={'broker':{'id':'selected_simulation'},'actions':{
            k:{'enabled':ready,'message':'blocked by backend'} for k in ('status','execute')}}
        client.request=Mock(side_effect=lambda path,data=None:self.state if path=='/api/state' else {'job_id':'shared-job'})
        return client

    def test_account_check_uses_existing_selection_and_shared_job_endpoint(self):
        client=self.client()
        self.assertEqual(client.operate('status')['job_id'],'shared-job')
        client.request.assert_any_call('/api/jobs',{'action':'status'})

    def test_execute_requires_exact_selected_broker_confirmation(self):
        client=self.client()
        for value in (None,'tiger_paper','futu_paper'):
            with self.assertRaises(ControlError):client.operate('execute',value)
        self.assertTrue(all(c.args[0]=='/api/state' for c in client.request.call_args_list))
        client.operate('execute','selected_simulation')
        client.request.assert_called_with('/api/jobs',{'action':'execute','confirmation':'selected_simulation'})

    def test_timeout_does_not_retry_mutation(self):
        client=self.client()
        client.request.side_effect=[self.state,ControlError('unknown outcome')]
        with self.assertRaises(ControlError):client.operate('execute','selected_simulation')
        self.assertEqual(client.request.call_count,2)

    def test_backend_blocker_prevents_any_job_post(self):
        client=self.client(False)
        with self.assertRaisesRegex(ControlError,'blocked by backend'):client.operate('status')
        client.request.assert_called_once_with('/api/state')

    def test_broker_status_without_explicit_profile_uses_workbench(self):
        import bot
        with patch.object(sys,'argv',['bot.py','broker-status','--connect']), patch('app.workbench_cli.main',return_value=0) as main:
            self.assertEqual(bot.main(),0)
            main.assert_called_once_with(['status'])

    def test_presentation_and_broker_layers_do_not_import_each_other_or_sql(self):
        root=Path(__file__).resolve().parents[1]
        for folder in ('portal','brokers'):
            for path in (root/folder).glob('*.py'):
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names=([n.name for n in node.names] if isinstance(node,ast.Import)
                           else [node.module or ''] if isinstance(node,ast.ImportFrom) else [])
                    forbidden={'brokers','sqlite3'} if folder=='portal' else {'portal'}
                    self.assertFalse(any(n.split('.')[0] in forbidden for n in names),path.name)
