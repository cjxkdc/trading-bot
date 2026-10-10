"""Local portal boundaries and research workflow. No vendor connection or order is sent."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from portal.app import create_app
from portal.service import MAX_FILE, PortalError, PortalService, atomic_json
from data.snapshot import demo_snapshot
from app.portal_worker import perform

ROOT = Path(__file__).resolve().parents[1]


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        shutil.copytree(ROOT / 'configs', self.root / 'configs')
        # These tests import demo_snapshot(), which uses the technology schema.
        # Do not depend on the user's currently selected strategy (e.g. MOM8).
        config_path = self.root / 'configs/portfolio_config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['strategy'] = {'type': 'tech_profit_momentum'}
        atomic_json(config_path, config)
        for folder in ('strategies', 'framework'):
            shutil.copytree(ROOT / folder, self.root / folder, ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('data', 'brokers'):
            shutil.copytree(ROOT / folder / 'plugins', self.root / folder / 'plugins')
        self.root_patch = patch('app.assembly.ROOT', self.root)
        self.root_patch.start()
        self.calls = []
        self.response = {'status': 'connected', 'message': 'test check',
                         'details': {'account_environment': 'SIMULATE', 'cash': '1000', 'positions': []}}
        self.wait = None

        def worker(*args, **kwargs):
            self.calls.append((args, kwargs))
            if self.wait:
                self.wait.wait(3)
            return deepcopy(self.response)

        self.service = PortalService(self.root, worker=worker)
        self.client = TestClient(create_app(self.root, service=self.service), base_url='http://127.0.0.1:8765')
        self.token = self.client.get('/api/session').json()['token']
        self.client.headers['X-Portal-Token'] = self.token

    def tearDown(self):
        if self.wait:
            self.wait.set()
        if self.service.thread:
            self.service.thread.join(4)
        self.client.close()
        self.root_patch.stop()
        self.temp.cleanup()

    def finish(self):
        self.service.thread.join(4)
        self.assertFalse(self.service.thread.is_alive())

    def test_opening_portal_does_not_connect_or_create_a_trading_ledger(self):
        state = self.client.get('/api/state').json()
        self.assertEqual(state['mode'], 'broker_paper')
        self.assertIsNone(state['account'])
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'runs/futu-paper').exists())
        self.assertIsNone(state['preview'])

    def test_cross_site_token_leak_and_dns_rebinding_are_rejected(self):
        for headers in ({'Origin': 'https://evil.example'}, {'Host': 'evil.example'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}):
            self.assertEqual(self.client.get('/api/session', headers=headers).status_code, 403)
            self.assertEqual(self.client.post('/api/jobs', headers=headers, json={'action': 'status'}).status_code, 403)
        self.assertFalse(self.calls)

    def test_external_strategy_edit_invalidates_uploaded_snapshot_without_breaking_state(self):
        self.service.select_source('snapshot_file')
        self.service.upload_snapshot(json.dumps(demo_snapshot()).encode())
        self.assertIsNotNone(self.service.state()['snapshot'])
        path = self.root / 'configs/portfolio_config.json'
        cfg = json.loads(path.read_text(encoding='utf-8'))
        mom = json.loads((self.root / 'configs/mom8.json').read_text(encoding='utf-8'))
        cfg['strategy'] = mom['strategy']
        atomic_json(path, cfg)
        response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertEqual(state['data_requirements']['universe'], 'all')
        self.assertIsNone(state['snapshot'])
        self.assertIsNone(state['preview'])
        self.assertIsNone(self.service.selected_snapshot)
        self.assertFalse(self.calls)

    def test_api_requires_session_and_private_files_are_not_served(self):
        for token in ('', 'wrong'):
            self.assertEqual(self.client.post('/api/jobs', headers={'X-Portal-Token': token},
                                            json={'action': 'status'}).status_code, 403)
        for path in ('/secrets/futu/connection.json', '/portfolio_config.json', '/configs/portfolio_config.json', '/runs/portal/server.json', '/api/../secrets/futu/connection.json'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get('/api/jobs').status_code, 405)
        self.assertIn("frame-ancestors 'none'", self.client.get('/api/state').headers['content-security-policy'])

    def test_size_and_format_checks_precede_mutations(self):
        self.assertEqual(self.client.post('/api/snapshot', content=b'x', headers={'Content-Length': str(MAX_FILE + 1)}).status_code, 413)
        self.assertEqual(self.client.post('/api/jobs', content='{"action":"status"}', headers={'Content-Type': 'text/plain'}).status_code, 409)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'futu_live'}).status_code, 409)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'configure-data'}).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_repointing_profile_to_live_cannot_gain_execution_permission(self):
        atomic_json(self.root / 'configs/futu_paper_config.json', {'extends': 'portfolio_config.json',
                    'broker': {'type': 'futu_live', 'config_dir': 'secrets/futu', 'state_dir': 'runs/futu-paper'}})
        state = self.client.get('/api/state').json()
        self.assertEqual(state['broker']['mode'], 'live')
        self.assertFalse(state['broker']['execution_enabled'])
        response = self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': 'futu_paper'})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.calls)

    def test_no_concurrent_worker_or_strategy_change(self):
        self.wait = threading.Event()
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'status'}).status_code, 200)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'refresh'}).status_code, 409)
        self.assertEqual(self.client.post('/api/source', json={'source': 'futu'}).status_code, 409)
        cfg, version = self.service.config()
        self.assertEqual(self.client.post('/api/strategy', json={'strategy': cfg['strategy'], 'version': version}).status_code, 409)
        self.wait.set()
        self.finish()
        self.assertEqual(len(self.calls), 1)

    def test_strategy_change_keeps_broker_and_risk_limits_and_invalidates_preview(self):
        before = json.loads((self.root / 'configs/portfolio_config.json').read_text(encoding='utf-8'))
        self.service.preview = {'execution_ready': True}
        _, version = self.service.config()
        result = self.client.post('/api/strategy', json={'version': version,
                    'strategy': {'type': 'equal_weight', 'count': 4, 'weight': .19}})
        self.assertEqual(result.status_code, 200, result.text)
        after = json.loads((self.root / 'configs/portfolio_config.json').read_text(encoding='utf-8'))
        for key in before.keys() - {'strategy'}:
            self.assertEqual(after[key], before[key])
        self.assertIsNone(self.service.preview)
        self.assertEqual(len(list((self.root / 'runs/portal/config-history').glob('*.json'))), 1)
        self.assertEqual(self.client.post('/api/strategy', json={'version': version, 'strategy': before['strategy']}).status_code, 409)

    def test_invalid_strategy_and_weights_cannot_bypass_existing_limits(self):
        _, version = self.service.config()
        for strategy in ({'type': 'equal_weight', 'count': 5, 'weight': .2},
                         {'type': 'equal_weight', 'count': 10, 'weight': .1},
                         {'type': 'buy_and_hold'}, {'type': '../../futu_live'}):
            response = self.client.post('/api/strategy', json={'version': version, 'strategy': strategy})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.service.config()[1], version)

    def test_latest_pointer_cannot_read_external_files(self):
        other = self.root / 'private.json'
        other.write_text(json.dumps(demo_snapshot()))
        directory = self.service.registry.control('data', 'massive').directory
        atomic_json(directory / 'latest.json', {'path': str(other)})
        state = self.client.get('/api/state').json()
        self.assertIsNone(state['snapshot'])
        self.assertIsNotNone(state['sources'][0]['issue'])
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'preview', 'snapshot': str(other)}).status_code, 409)

    def test_snapshot_upload_is_local_and_does_not_trade(self):
        self.service.select_source('snapshot_file')
        response = self.client.post('/api/snapshot', content=json.dumps(demo_snapshot()).encode())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('path', response.json())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.client.get('/api/state').json()['snapshot']['synthetic'])
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': self.service.broker_id}).status_code, 409)

    def test_research_preview_never_grants_execution(self):
        self.service.select_source('snapshot_file')
        self.service.upload_snapshot(json.dumps(demo_snapshot()).encode())
        self.response = {'status': 'previewed', 'message': 'research only', 'targets': [],
                         'execution_ready': False, 'preview_token': 'must-not-leak'}
        self.service.start('preview')
        self.finish()
        response = self.client.get('/api/state')
        self.assertNotIn('must-not-leak', response.text)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': self.service.broker_id}).status_code, 409)

    def test_execute_consumes_capability_once_and_never_accepts_a_client_path(self):
        self.service.select_source('snapshot_file')
        self.service.upload_snapshot(json.dumps(demo_snapshot()).encode())
        self.response = {'status': 'previewed', 'message': 'fixture', 'execution_ready': True, 'preview_token': 'server-only'}
        self.service.start('preview'); self.finish()
        self.service.account = {'status': 'connected'}
        self.service.broker_session.verify(self.service.broker_id,
            self.service.registry.connection_context(self.service.broker_id))
        self.response = {'status': 'paused', 'message': 'unknown outcome'}
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute'}).status_code, 409)
        response = self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': self.service.broker_id,
                                                      'snapshot': 'C:/outside.json', 'preview_token': 'wrong'})
        self.assertEqual(response.status_code, 200)
        self.finish()
        args, _ = self.calls[-1]
        self.assertTrue(Path(args[2]).is_relative_to(self.root / 'runs/portal/snapshots'))
        self.assertEqual(args[3], 'server-only')
        self.assertIsNone(self.service.account)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': self.service.broker_id}).status_code, 409)
        self.assertEqual(len(self.calls), 2)
        all_logs = ''.join(p.read_text(encoding='utf-8') for p in (self.root / 'runs/portal/jobs').glob('*.json'))
        self.assertNotIn('server-only', all_logs)

    def test_credentials_are_never_persisted_in_job_log(self):
        credentials = {'api_key': 'fixture-secret-123456789', 'requests_per_minute': 5}
        self.response = {'status': 'ready', 'message': 'saved', 'details': {'api_key': credentials['api_key']}}
        self.service.start('configure-data', credentials=credentials); self.finish()
        self.assertEqual(credentials, {})
        self.assertNotIn('fixture-secret', self.client.get('/api/state').text)
        for p in (self.root / 'runs/portal/jobs').glob('*.json'):
            self.assertNotIn('fixture-secret', p.read_text(encoding='utf-8'))

    def test_cancel_only_after_collector_announces_progress(self):
        self.wait = threading.Event()
        with patch('data.massive.configuration_status', return_value={'configured': True}):
            self.service.start('refresh')
        self.assertEqual(self.client.post('/api/cancel-download', json={}).status_code, 409)
        self.calls[-1][0][4]('download started')
        self.assertEqual(self.client.post('/api/cancel-download', json={}).status_code, 200)
        self.assertTrue((self.root / 'runs/data/massive/cache/cancel.request').exists())
        self.wait.set(); self.finish()

    def test_orders_read_existing_database_without_changing_it(self):
        path = self.root / 'runs/futu-paper/journal.sqlite'
        self.assertEqual(self.service.orders()['items'], [])
        self.assertFalse(path.exists())
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE intents (id TEXT, cycle TEXT, body TEXT, status TEXT, state TEXT)')
            db.execute('INSERT INTO intents VALUES (?, ?, ?, ?, ?)', ('id', 'cycle', json.dumps(
                {'symbol': 'TEST', 'side': 'buy', 'quantity': '2', 'limit_price': '10'}), 'filled',
                json.dumps({'filled_quantity': '2', 'average_price': '9.9'})))
        db.close()
        before = path.read_bytes()
        result = self.client.get('/api/orders').json()
        self.assertEqual(result['items'][0]['filled_quantity'], '2')
        self.assertEqual(path.read_bytes(), before)

    def test_restart_never_replays_jobs_or_restores_execution_capabilities(self):
        atomic_json(self.service.folder / 'jobs' / 'unfinished.json',
                    {'id': 'unfinished', 'status': 'running', 'action': 'execute', 'started_at': '2020'})
        resumed = PortalService(self.root, worker=lambda *a, **kw: self.fail('must not run'))
        self.assertEqual(resumed.jobs[0]['status'], 'interrupted')
        self.assertIsNone(resumed.preview_private)
        self.assertIsNone(resumed.account)

    def test_backtest_export_import_and_compare_use_frozen_evidence(self):
        ids = []
        for slip in (10, 20):
            response = self.client.post('/api/experiments', json={'start': '2020-01-01', 'end': '2020-12-31',
                                                                'slippage_bps': slip})
            self.assertEqual(response.status_code, 200, response.text)
            ident = response.json()['id']
            self.assertIn('QCAlgorithm', self.client.get(f'/api/experiments/{ident}/code').text)
            fixture = {'status': 'Completed.', 'completed': True, 'periodStart': '2020-01-01',
                       'periodFinish': '2020-12-31', 'statistics': {'Net Profit': '5%',
                       'Compounding Annual Return': '5%', 'Drawdown': '3%', 'Total Orders': '2'}}
            imported = self.client.post(f'/api/experiments/{ident}/result', content=json.dumps(fixture).encode())
            self.assertEqual(imported.status_code, 200, imported.text)
            ids.append(imported.json()['id'])
        report = self.client.post('/api/compare', json={'ids': ids})
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()['comparison_differences'], ['slippage_bps'])
        self.assertEqual(report.json()['rows'][0]['code_evidence'], 'source_unverified')
        result = self.service.folder / 'experiments' / ids[0] / 'summary.json'
        record = json.loads(result.read_text(encoding='utf-8'))
        record['result']['metrics']['total_return'] = 10
        atomic_json(result, record)
        self.assertEqual(self.client.post('/api/compare', json={'ids': ids}).status_code, 422)
        self.assertEqual(self.client.post('/api/compare', json={'ids': ['../../secrets', ids[1]]}).status_code, 409)

    def register_alternatives(self):
        """Only manifests change; all aliases reuse compatible module implementations."""
        entries = {}
        for folder, original, alias in [('data', 'snapshot_file', 'lab_data'),
                                        ('brokers', 'paper', 'lab_broker'),
                                        ('strategies', 'equal_weight', 'lab_strategy')]:
            entry = json.loads((self.root / folder / 'plugins' / (original + '.json')).read_text(encoding='utf-8'))
            entry.update(type=alias, display_name='实验 ' + alias)
            if folder == 'data':
                entry['control']['default_profile']['directory'] = 'runs/data/lab-data'
            elif folder == 'brokers':
                entry['control']['default_profile']['state_dir'] = 'runs/lab-broker'
            else:
                entry['parameters']['count']['label'] = '实验组合数量'
            atomic_json(self.root / folder / 'plugins' / (alias + '.json'), entry)
            entries[alias] = entry
        return entries

    def test_new_manifests_appear_and_switch_without_portal_or_framework_changes(self):
        self.register_alternatives()
        state = self.client.get('/api/state').json()
        self.assertIn('lab_data', [p['id'] for p in state['sources']])
        self.assertIn('lab_broker', [p['id'] for p in state['brokers']])
        strategy = next(p for p in state['strategies'] if p['type'] == 'lab_strategy')
        self.assertEqual(strategy['name'], '实验 lab_strategy')
        self.assertEqual(next(f for f in strategy['fields'] if f['name'] == 'count')['label'], '实验组合数量')
        original = json.loads((self.root / 'configs/portfolio_config.json').read_text(encoding='utf-8'))
        self.assertEqual(self.client.post('/api/source', json={'source': 'lab_data'}).status_code, 200)
        self.assertEqual(self.client.post('/api/broker', json={'broker': 'lab_broker'}).status_code, 200)
        response = self.client.post('/api/strategy', json={'version': state['config_version'],
            'strategy': {'type': 'lab_strategy', 'count': 4, 'weight': .19}})
        self.assertEqual(response.status_code, 200, response.text)
        saved = json.loads((self.root / 'configs/portfolio_config.json').read_text(encoding='utf-8'))
        for field in original.keys() - {'strategy'}:
            self.assertEqual(saved[field], original[field])
        self.service.start('status'); self.finish()
        self.assertEqual(self.calls[-1][1]['broker_id'], 'lab_broker')
        self.assertEqual(self.calls[-1][1]['source_id'], 'lab_data')
        resumed = PortalService(self.root)
        self.assertEqual((resumed.broker_id, resumed.source), ('lab_broker', 'lab_data'))

    def test_switching_broker_clears_previous_account_preview_and_ledger_view(self):
        self.register_alternatives()
        self.service.account = {'status': 'connected'}
        self.service.preview = {'execution_ready': True}
        self.service.preview_private = ('old-file', 'old-capability')
        self.service.select_broker('lab_broker')
        self.assertIsNone(self.service.account)
        self.assertIsNone(self.service.preview_private)
        self.assertEqual(self.service.orders()['items'], [])
        self.assertFalse((self.root / 'runs/lab-broker').exists())
        self.assertFalse((self.root / 'runs/futu-paper').exists())
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': 'futu_paper'}).status_code, 409)
        self.assertFalse(self.calls)

    def test_new_local_broker_executes_only_in_its_own_temporary_ledger(self):
        self.register_alternatives()
        self.service.select_broker('lab_broker')
        self.service.select_source('lab_data')
        self.service.upload_snapshot(json.dumps(demo_snapshot()).encode())
        path = self.service.snapshots[self.service.selected_snapshot]
        args = dict(root=self.root, broker_id='lab_broker', source_id='lab_data', snapshot_path=path)
        preview = perform('preview', **args)
        self.assertTrue(preview['execution_ready'], preview)
        self.assertFalse((self.root / 'runs/lab-broker').exists())
        result = perform('execute', preview_token=preview['preview_token'], **args)
        self.assertEqual(result['status'], 'complete', result)
        self.assertTrue((self.root / 'runs/lab-broker/journal.sqlite').is_file())
        orders = self.service.orders()['items']
        self.assertTrue(orders)
        self.assertTrue(all(o['symbol'].startswith('DEMO-') for o in orders))
        count = len(orders)
        perform('execute', preview_token=preview['preview_token'], **args)
        self.assertEqual(len(self.service.orders()['items']), count)
        self.assertFalse((self.root / 'runs/futu-paper').exists())
        inspected = perform('status', **args)
        self.assertEqual(inspected['status'], 'connected')
        self.assertFalse(inspected['details']['network_requested'])

    def test_live_modules_can_be_selected_but_selection_does_not_authorize_execution(self):
        state = self.client.get('/api/state').json()
        live = [p for p in state['brokers'] if p.get('mode') == 'live']
        self.assertTrue(live)
        for item in live:
            self.assertTrue(item['enabled'])
            self.assertFalse(item['execution_enabled'])
            self.assertEqual(self.client.post('/api/broker', json={'broker': item['id']}).status_code, 200)
            selected = self.client.get('/api/state').json()
            self.assertFalse(selected['actions']['execute']['enabled'])
        self.assertFalse(self.calls)
        self.assertEqual(self.service.broker_id, live[-1]['id'])

    def test_actual_connector_mode_cannot_be_relabelled_by_manifest(self):
        self.register_alternatives()
        path = self.root / 'brokers/plugins/lab_broker.json'
        entry = json.loads(path.read_text(encoding='utf-8'))
        entry['control']['mode'] = 'broker_paper'  # Actual connector still reports paper.
        atomic_json(path, entry)
        with self.assertRaisesRegex(PortalError, '账户环境'):
            perform('status', root=self.root, source_id='snapshot_file', broker_id='lab_broker')

    def test_module_capabilities_and_input_schema_are_enforced(self):
        self.register_alternatives()
        self.service.select_source('lab_data')
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'refresh'}).status_code, 409)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'data-status'}).status_code, 409)
        self.assertFalse(self.calls)
        entry_path = self.root / 'data/plugins/lab_data.json'
        entry = json.loads(entry_path.read_text(encoding='utf-8'))
        entry['strategy_schemas'] = ['some_other_format_v1']
        atomic_json(entry_path, entry)
        self.assertIsNotNone(self.client.get('/api/state').json()['compatibility_issue'])
        with self.assertRaisesRegex(PortalError, '不兼容'):
            perform('preview', root=self.root, broker_id='paper', source_id='lab_data')

    def test_generic_settings_secret_fields_are_validated_and_redacted(self):
        # This fixture declares configure, so it must also supply that control method.
        configure = patch('data.request_control.SnapshotProviderControl.configure', create=True)
        configure.start()
        self.addCleanup(configure.stop)
        entries = self.register_alternatives()
        entry = entries['lab_data']
        entry['control']['capabilities'].append('configure')
        entry['control']['settings_fields'] = [
            {'name': 'access_token', 'label': '访问令牌', 'type': 'string', 'secret': True, 'max_length': 60},
            {'name': 'batch_size', 'label': '批大小', 'type': 'integer', 'min': 1, 'max': 50}]
        atomic_json(self.root / 'data/plugins/lab_data.json', entry)
        self.service.select_source('lab_data')
        secret = 'another-vendor-secret-123'
        self.response = {'status': 'ready', 'message': 'saved ' + secret,
                         'details': {'access_token': secret, 'nested': ['token=' + secret]}}
        response = self.client.post('/api/data/settings', json={'access_token': secret, 'batch_size': 5})
        self.assertEqual(response.status_code, 200, response.text)
        self.finish()
        self.assertNotIn(secret, self.client.get('/api/state').text)
        for file in (self.service.folder / 'jobs').glob('*.json'):
            self.assertNotIn(secret, file.read_text(encoding='utf-8'))
        self.assertEqual(self.calls[-1][1]['source_id'], 'lab_data')
        for payload in ({'api_key': 'unknown'}, {'batch_size': 51}, {'batch_size': True}):
            self.assertEqual(self.client.post('/api/data/settings', json=payload).status_code, 409)
        self.assertEqual(len(self.calls), 1)

    def test_connection_binding_changes_invalidate_execution_preview(self):
        self.service.select_source('snapshot_file')
        self.service.upload_snapshot(json.dumps(demo_snapshot()).encode())
        self.response = {'status': 'previewed', 'message': 'fixture', 'execution_ready': True, 'preview_token': 'private'}
        self.service.start('preview'); self.finish()
        self.service.account = {'status': 'connected'}
        self.assertTrue(self.service.preview_private)
        atomic_json(self.root / 'secrets/futu/connection.json', {'account_id': 'different-binding'})
        response = self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': 'futu_paper'})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.service.preview_private)
        self.assertEqual(len(self.calls), 1)

    def test_data_configuration_change_invalidates_research_preview(self):
        original = self.service.registry.context(self.service.broker_id, self.service.source)
        self.service.preview = {'execution_ready': False}
        self.service.preview_context = original
        atomic_json(self.root / 'secrets/massive/connection.json', {'api_key': 'replacement-key'})
        self.assertNotEqual(original, self.service.registry.context(self.service.broker_id, self.service.source))
        self.assertIsNone(self.client.get('/api/state').json()['preview'])


if __name__ == '__main__':
    unittest.main()
