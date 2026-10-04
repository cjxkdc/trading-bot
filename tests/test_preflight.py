"""Preparation composition: only read-only workers and no stale submission capability."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.operations import ACTIONS, WORKBENCH_ACTIONS, context_for
from app.preflight import READ_ONLY_ACTIONS
from app.readiness import operation_states
from app.workbench import PortalService, PortalError, atomic_json
from app.workbench_cli import WorkbenchClient
from data.snapshot import demo_snapshot
from portal.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for folder in ('configs', 'strategies', 'framework'):
            shutil.copytree(ROOT/folder, self.root/folder, ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('data', 'brokers'):
            shutil.copytree(ROOT/folder/'plugins', self.root/folder/'plugins')
        config_path = self.root/'configs/portfolio_config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['strategy'] = {'type':'tech_profit_momentum','count':5,'weight':.19}
        atomic_json(config_path, config)
        self.root_patch = patch('app.assembly.ROOT', self.root)
        self.root_patch.start()
        self.calls, self.hook, self.wait = [], None, None
        self.responses = {
            'status': dict(status='connected', message='模拟账户已连接。',
                           details={'account_id':'private-account', 'api_key':'private-key', 'cash':'1000'}),
            'data-status': dict(status='connected', message='接口可用。'),
            'refresh': dict(status='previewed', message='已复用缓存并更新。', execution_ready=False),
            'preview': dict(status='previewed', message='研究预览。', execution_ready=False,
                            targets=[{'asset_id':'TEST', 'weight':.1}], issues=[]),
            'prepare': dict(status='previewed', message='准备完成。', execution_ready=True,
                            preview_token='private-preview-capability', expires_at=9999999999,
                            targets=[{'asset_id':'TEST', 'weight':.1}], orders=[{'symbol':'TEST'}],
                            account_details={'cash':'1000'})}
        def worker(root, action, path, token, progress, **kwargs):
            self.assertIn(action, READ_ONLY_ACTIONS)
            self.assertIsNone(token)
            self.assertIsNone(kwargs['credentials'])
            self.calls.append((action, path, kwargs))
            self.assertEqual(kwargs['expected_context'], context_for(self.service.registry, action,
                             kwargs['broker_id'], kwargs['source_id']))
            if self.hook:
                self.hook(action, progress)
            result = deepcopy(self.responses[action])
            if action in {'refresh', 'preview', 'prepare'}:
                result['snapshot_path'] = str(self.path)
            return result
        self.service = PortalService(self.root, worker=worker)
        self.service.select_source('snapshot_file')
        meta = self.service.upload_snapshot(json.dumps(demo_snapshot()).encode())
        self.path = self.service.snapshots[meta['id']]
        self.client = TestClient(create_app(self.root, service=self.service), base_url='http://127.0.0.1:8765')
        self.client.headers['X-Portal-Token'] = self.client.get('/api/session').json()['token']

    def tearDown(self):
        if self.wait:
            self.wait.set()
        if self.service.thread:
            self.service.thread.join(5)
        self.client.close()
        self.root_patch.stop()
        self.temp.cleanup()

    def finish(self):
        self.service.thread.join(5)
        self.assertFalse(self.service.thread.is_alive())
        self.assertIsNone(self.service.active)
        return self.service.jobs[0]

    def run_job(self):
        response = self.client.post('/api/jobs', json={'action':'preflight'})
        self.assertEqual(response.status_code, 200, response.text)
        return self.finish()

    def collector_descriptor(self, kind, ident):
        desc = self.original_descriptor(kind, ident)
        if kind == 'data' and ident == 'snapshot_file':
            desc = dict(desc, capabilities=['preview','collect','inspect','cancel'])
        return desc

    def test_composite_not_a_worker_action_and_idle_preconditions_do_not_grey_it(self):
        self.assertIn('preflight', WORKBENCH_ACTIONS)
        self.assertNotIn('preflight', ACTIONS)
        state = operation_states(broker={'enabled':False,'mode':'broker_paper'}, source={'enabled':False},
            snapshot=None, preview=None, verified=False, account=None, preview_authorized=False,
            compatibility_issue='incompatible', context_issue='missing key', strategy_issue='unavailable')
        self.assertTrue(state['preflight']['enabled'])
        for gate in ('busy', 'closing'):
            state = operation_states(broker={}, source={}, snapshot=None, preview=None, verified=False,
                account=None, preview_authorized=False, **{gate:True})
            self.assertFalse(state['preflight']['enabled'])

    def test_one_job_read_only_pipeline_installs_normal_private_preview(self):
        job = self.run_job()
        self.assertEqual([c[0] for c in self.calls], ['status','preview','prepare'])
        self.assertEqual(len(self.service.jobs), 1)
        self.assertEqual(job['status'], 'previewed')
        self.assertEqual([s['status'] for s in job['steps']], ['passed','passed','skipped','passed','passed'])
        self.assertEqual(self.service.preview_private, (self.path, 'private-preview-capability'))
        view = self.service.state()
        self.assertTrue(view['broker_verified'])
        self.assertTrue(view['actions']['execute']['enabled'])
        self.assertEqual(view['preflight_job']['id'], job['id'])
        data = json.dumps(view) + ''.join(p.read_text(encoding='utf-8') for p in (self.service.folder/'jobs').glob('*.json'))
        for secret in ('private-preview-capability','private-account','private-key'):
            self.assertNotIn(secret, data)
        self.assertFalse(list(self.root.rglob('journal.sqlite')))

    def test_weekend_prepare_is_blocked_after_prior_checks_without_execution_authority(self):
        self.responses['prepare'] = dict(status='previewed', execution_ready=False,
            message='当前不在美股正常交易时段；开盘后重新准备交易。', blockers=['当前不在美股正常交易时段。'])
        job = self.run_job()
        self.assertEqual(job['status'], 'blocked')
        self.assertEqual([s['status'] for s in job['steps']], ['passed','passed','skipped','passed','blocked'])
        self.assertIn('正常交易时段', job['steps'][-1]['message'])
        self.assertIsNone(self.service.preview_private)
        self.assertFalse(self.service.state()['actions']['execute']['enabled'])

    def test_account_failure_clears_old_capability_and_skips_dependents(self):
        self.service.preview = {'execution_ready':True}
        self.service.preview_private = (self.path, 'old')
        self.responses['status'] = dict(status='blocked', message='请先登录券商客户端。')
        job = self.run_job()
        self.assertEqual([c[0] for c in self.calls], ['status'])
        self.assertEqual([s['status'] for s in job['steps']], ['blocked','skipped','skipped','skipped','skipped'])
        self.assertIsNone(self.service.preview_private)
        self.assertIsNone(self.service.account)
        self.assertFalse(self.service.state()['broker_verified'])

    def test_missing_file_reports_import_instead_of_fragment_or_worker_preview(self):
        self.service.selected_snapshot = None
        job = self.run_job()
        self.assertIn('导入完整快照', job['message'])
        self.assertEqual([c[0] for c in self.calls], ['status'])
        self.assertEqual(job['steps'][2]['status'], 'blocked')
        self.assertEqual(job['steps'][3]['status'], 'skipped')

    def test_unconfigured_source_blocks_data_step_after_account(self):
        with patch('data.request_control.SnapshotProviderControl.configuration_status', return_value={'configured':False}):
            job = self.run_job()
        self.assertIn('配置当前数据源', job['message'])
        self.assertEqual([c[0] for c in self.calls], ['status'])
        self.assertEqual(job['steps'][1]['status'], 'blocked')

    def test_collector_uses_existing_refresh_with_same_context_and_warns_about_coverage(self):
        self.original_descriptor = self.service.registry.descriptor
        self.responses['refresh']['issues'] = ['覆盖不完整']
        self.responses['preview']['issues'] = ['覆盖不完整']
        self.responses['prepare']['issues'] = ['覆盖不完整']
        with patch.object(self.service.registry, 'descriptor', side_effect=self.collector_descriptor):
            job = self.run_job()
        self.assertEqual([c[0] for c in self.calls], ['status','data-status','refresh','preview','prepare'])
        self.assertIn('数据限制', job['steps'][2]['message'])
        self.assertIn('数据限制', job['message'])
        self.assertEqual(job['result']['issues'], ['覆盖不完整'])

    def test_configuration_change_during_prepare_discards_result_and_current_job(self):
        def hook(action, progress):
            if action == 'prepare':
                path = self.root/'configs/portfolio_config.json'
                raw = json.loads(path.read_text(encoding='utf-8'))
                raw['strategy'] = {'type':'equal_weight','count':4,'weight':.19}
                atomic_json(path, raw)
        self.hook = hook
        job = self.run_job()
        self.assertEqual(job['status'], 'blocked')
        self.assertIsNone(self.service.preview)
        self.assertIsNone(self.service.preview_private)
        self.assertIsNone(self.service.account)
        self.assertIsNone(self.service.state()['preflight_job'])

    def test_no_orders_is_complete_not_permission_to_execute(self):
        self.responses['prepare'] = dict(status='complete', no_orders=True, execution_ready=False,
                                         message='差额不足一股。', orders=[])
        job = self.run_job()
        self.assertEqual(job['status'], 'complete')
        self.assertTrue(job['result']['no_orders'])
        self.assertEqual(job['steps'][-1]['status'], 'passed')
        self.assertIsNone(self.service.preview_private)
        self.assertFalse(self.service.state()['actions']['execute']['enabled'])

    def test_active_preflight_serializes_every_phase_and_blocks_module_changes(self):
        reached = threading.Event()
        self.wait = threading.Event()
        def hook(action, progress):
            if action == 'status':
                reached.set()
                self.wait.wait(4)
        self.hook = hook
        self.service.start('preflight')
        self.assertTrue(reached.wait(2))
        with self.assertRaises(PortalError):
            self.service.start('preflight')
        with self.assertRaises(PortalError):
            self.service.select_source('massive')
        with self.assertRaises(PortalError):
            self.service.select_broker('paper')
        self.wait.set()
        self.finish()
        self.assertEqual(len(self.service.jobs), 1)

    def test_cancelled_collection_never_advances_even_if_collector_returns_success(self):
        reached = threading.Event()
        self.wait = threading.Event()
        def hook(action, progress):
            if action == 'refresh':
                progress('更新缓存中')
                reached.set()
                self.wait.wait(4)
                progress('采集器已经返回')
        self.hook = hook
        self.original_descriptor = self.service.registry.descriptor
        with patch.object(self.service.registry, 'descriptor', side_effect=self.collector_descriptor), \
             patch('data.request_control.SnapshotProviderControl.cancel', create=True) as cancel:
            self.service.start('preflight')
            self.assertTrue(reached.wait(2))
            self.assertTrue(self.service.active['cancel_available'])
            self.service.cancel_download()
            self.wait.set()
            job = self.finish()
            cancel.assert_called_once()
        self.assertEqual([c[0] for c in self.calls], ['status','data-status','refresh'])
        self.assertEqual(job['status'], 'blocked')
        self.assertIn('已停止', job['message'])
        self.assertIsNone(self.service.preview_private)

    def test_restart_retains_steps_without_replaying_or_restoring_capability(self):
        job = self.run_job()
        resumed = PortalService(self.root, worker=lambda *a, **kw:self.fail('must not execute'))
        self.assertEqual(resumed.jobs[0]['steps'], job['steps'])
        self.assertIsNone(resumed.preview_private)
        self.assertIsNone(resumed.account)

    def test_interrupted_job_shows_stopped_steps_and_never_resumes(self):
        atomic_json(self.service.folder/'jobs'/'interrupted.json', dict(id='interrupted', action='preflight',
            status='running', started_at='2099', cancel_available=True,
            steps=[dict(id='account', status='passed'), dict(id='data', status='running'),
                   dict(id='refresh', status='pending')]))
        resumed = PortalService(self.root, worker=lambda *a, **kw:self.fail('must not resume'))
        job = resumed.jobs[0]
        self.assertEqual(job['status'], 'interrupted')
        self.assertEqual([s['status'] for s in job['steps']], ['passed','blocked','skipped'])
        self.assertFalse(job['cancel_available'])

    def test_actual_local_paper_preview_remains_read_only_and_can_be_confirmed_later(self):
        from app.portal_worker import perform
        self.service.select_broker('paper')
        actual_calls = []
        def worker(root, action, path, token, progress, **kwargs):
            actual_calls.append(action)
            self.assertIn(action, {'status','preview'})
            return perform(action, root=root, snapshot_path=path, preview_token=token,
                           progress=progress, **kwargs)
        self.service.worker = worker
        job = self.run_job()
        self.assertEqual(actual_calls, ['status','preview'])
        self.assertEqual(job['status'], 'previewed')
        self.assertTrue(self.service.preview['execution_ready'])
        self.assertIsNotNone(self.service.preview_private)
        self.assertTrue(self.service.state()['actions']['execute']['enabled'])
        self.assertFalse(list(self.root.rglob('journal.sqlite')))

    def test_prior_data_warnings_survive_a_later_prepare_blocker(self):
        self.responses['preview']['issues'] = ['候选覆盖有限']
        self.responses['prepare'] = dict(status='blocked', message='休市，无法取得可交易报价。')
        job = self.run_job()
        self.assertEqual(job['status'], 'blocked')
        self.assertIn('候选覆盖有限', job['result']['issues'])
        self.assertEqual(job['steps'][3]['warnings'], ['候选覆盖有限'])
        self.assertIsNone(self.service.preview_private)

    def test_untrusted_exception_is_not_logged_and_later_steps_skipped(self):
        self.hook = lambda action, progress: (_ for _ in ()).throw(RuntimeError('sdk-password-raw'))
        job = self.run_job()
        self.assertEqual(job['status'], 'blocked')
        self.assertNotIn('sdk-password-raw', json.dumps(job))
        self.assertIsNone(self.service.preview_private)

    def test_read_only_inspection_rejects_live_module_before_any_worker(self):
        self.original_descriptor = self.service.registry.descriptor
        def descriptor(kind, ident):
            result = self.original_descriptor(kind, ident)
            return dict(result, mode='live', enabled=False) if kind == 'broker' else result
        with patch.object(self.service.registry, 'descriptor', side_effect=descriptor):
            job = self.run_job()
        self.assertEqual(job['status'], 'blocked')
        self.assertFalse(self.calls)

    def test_cli_submits_composite_once_without_trade_confirmation(self):
        client = WorkbenchClient(url='http://127.0.0.1:12345')
        with patch.object(client, 'request', side_effect=[{'actions':{'preflight':{'enabled':True}}}, {'job_id':'one'}]) as request:
            self.assertEqual(client.operate('preflight'), {'job_id':'one'})
        self.assertEqual(request.call_args_list[-1].args, ('/api/jobs', {'action':'preflight'}))


if __name__ == '__main__':
    unittest.main()
