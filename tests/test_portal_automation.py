"""Portal scheduling boundaries exercised without brokerage or network traffic."""
import json
import threading
import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from portal.access import RemoteSettings, password_record
from portal.app import create_app
from portal.controller import create_controller
from portal.runtime import Reply
import test_portal as portal_fixture


class InProcessRuntime:
    """Send the authenticated controller to the real API without opening a socket."""
    def __init__(self, backend):
        self.backend, self.calls = backend, []

    def forward(self, method, path, content=None, content_type=None):
        self.calls.append((method, path))
        headers = {'Content-Type': content_type} if content_type else {}
        reply = self.backend.request(method, path, content=content, headers=headers)
        return Reply(reply.status_code, reply.content,
                     {'content-type': reply.headers.get('content-type', 'application/json')})


class PortalAutomationTests(unittest.TestCase):
    def setUp(self):
        # Reuse only fixture setup; importing/inheriting its TestCase would run all
        # legacy Portal tests again during discovery.
        portal_fixture.PortalTests.setUp(self)
        self.configured = patch('data.massive.configuration_status', return_value={'configured': True})
        self.configured.start()

    def tearDown(self):
        self.service.stop_scheduler()
        self.configured.stop()
        portal_fixture.PortalTests.tearDown(self)

    def finish(self):
        portal_fixture.PortalTests.finish(self)

    def verify(self):
        reply = self.client.post('/api/jobs', json={'action': 'status'})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.finish()
        self.assertTrue(self.client.get('/api/state').json()['broker_verified'])

    def arm(self):
        self.verify()
        reply = self.client.post('/api/automation/enable', json={})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(self.client.get('/api/state').json()['automation']['enabled'])

    def test_view_is_offline_and_automation_is_off_by_default(self):
        for _ in range(2):
            reply = self.client.get('/api/state')
            self.assertEqual(reply.status_code, 200)
            self.assertFalse(reply.json()['automation']['enabled'])
            self.assertEqual(reply.json()['automation']['status'], 'disabled')
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.service.thread)
        self.assertFalse(self.service.automation.path.exists())

    def test_enable_wakes_scheduler_and_publishes_first_check_without_prior_account_click(self):
        checked = threading.Event()
        actions = []
        self.service.automation.clock = lambda: datetime(2026,10,3,12,tzinfo=ZoneInfo('America/New_York'))
        def worker(root, action, *args, **kwargs):
            actions.append(action)
            if action == 'calendar':
                return {'status':'ready','message':'calendar','sessions':['2026-10-02','2026-10-05','2026-11-02']}
            if action == 'status':
                return {'status':'connected','message':'account','details':{'currency':'USD','cash':'1000'}}
            if action == 'refresh':
                checked.set()
                return {'status':'previewed','message':'selection','targets':[{'asset_id':'TEST','weight':.1}],
                        'execution_ready':False,'issues':[]}
            self.fail('unexpected action ' + action)
        self.service.worker = worker
        self.service.start_scheduler()
        reply = self.client.post('/api/automation/enable', json={})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(checked.wait(3), 'enable did not wake the scheduler')
        self.finish()
        state = self.client.get('/api/state').json()
        self.assertEqual(actions, ['calendar','status','refresh'])
        self.assertEqual(state['automation']['startup']['status'], 'checked')
        self.assertEqual(state['automation']['next_run'][:10], '2026-10-05')
        self.assertEqual(state['account']['status'], 'connected')
        self.assertEqual(state['preview']['targets'][0]['asset_id'], 'TEST')
        self.assertFalse(state['preview']['execution_ready'])
        self.assertIsNone(self.service.preview_private)

    def test_new_routes_require_session_and_reject_cross_origin_mutations(self):
        for action, value in [('config', {'day': 2}), ('enable', {}), ('disable', {})]:
            for headers in ({'X-Portal-Token': ''}, {'X-Portal-Token': 'wrong'},
                            {'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(action=action, headers=headers):
                    reply = self.client.post('/api/automation/' + action, json=value, headers=headers)
                    self.assertEqual(reply.status_code, 403)
        self.assertFalse(self.service.automation.path.exists())
        self.assertEqual(self.calls, [])

    def test_remote_owner_login_is_required_before_schedule_controls(self):
        origin = 'https://bot.example'
        password = 'Offline fixture password 2026!'
        runtime = InProcessRuntime(self.client)
        settings = RemoteSettings(origin, password_record(password))
        controller = create_controller(self.root, port=5329, instance='automation-test',
                                       runtime=runtime, access=settings)
        with TestClient(controller, base_url=origin) as remote:
            token = remote.get('/api/auth/status').json()['login_token']
            remote.headers.update({'Origin': origin, 'X-Portal-Token': token})
            for path in ('config', 'enable', 'disable'):
                self.assertEqual(remote.post('/api/automation/' + path, json={}).status_code, 401)
            self.assertFalse(runtime.calls)
            self.assertEqual(remote.post('/api/auth/login', json={'username': 'owner', 'password': password}).status_code, 200)
            reply = remote.post('/api/automation/config', json={'day': 2, 'trade_time': '10:00'})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(self.service.automation.data['config']['day'], 2)
            self.verify()
            self.assertEqual(remote.post('/api/automation/enable', json={}).status_code, 200)
            self.assertTrue(self.service.automation.data['enabled'])
            self.assertEqual(remote.post('/api/automation/disable', json={}).status_code, 200)
            self.assertFalse(self.service.automation.data['enabled'])
            # A valid owner cookie still cannot omit the CSRF token.
            self.assertEqual(remote.post('/api/automation/enable', json={},
                                         headers={'X-Portal-Token': ''}).status_code, 403)
        self.assertNotIn('execute', [args[1] for args, _ in self.calls])

    def test_unverified_account_gets_startup_check_but_local_replay_cannot_enable(self):
        reply = self.client.post('/api/automation/enable', json={})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertFalse(self.calls)
        self.assertTrue(self.service.automation.data['enabled'])
        self.assertEqual(self.service.automation.data['startup']['status'], 'pending_check')
        self.assertTrue(self.service.scheduler_wake.is_set())
        self.client.post('/api/automation/disable', json={})
        reply = self.client.post('/api/broker', json={'broker': 'paper'})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.verify()
        reply = self.client.post('/api/automation/enable', json={})
        self.assertEqual(reply.status_code, 409)
        self.assertFalse(self.service.automation.data['enabled'])
        self.assertEqual([args[1] for args, _ in self.calls], ['status'])

    def test_schedule_payload_cannot_enable_or_choose_another_account(self):
        for value in ({'enabled': True}, {'broker_id': 'live'}, {'day': 31}):
            self.assertEqual(self.client.post('/api/automation/config', json=value).status_code, 409)
        for path in ('enable', 'disable'):
            self.assertEqual(self.client.post('/api/automation/' + path,
                                             json={'broker_id': 'other'}).status_code, 409)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.service.automation.data['enabled'])

    def test_disable_is_allowed_during_an_active_job_and_revokes_generation(self):
        self.arm()
        generation = self.service.automation.data['generation']
        self.wait = threading.Event()
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'status'}).status_code, 200)
        self.assertIsNotNone(self.service.active)
        reply = self.client.post('/api/automation/disable', json={})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertFalse(self.service.automation.data['enabled'])
        stored = json.loads(self.service.automation.path.read_text(encoding='utf-8'))
        self.assertFalse(stored['enabled'])
        self.assertNotEqual(stored['generation'], generation)
        self.assertTrue(self.service.thread.is_alive())
        self.wait.set()
        self.finish()
        self.assertEqual([args[1] for args, _ in self.calls], ['status', 'status'])

    def test_armed_plan_blocks_manual_execution_and_schedule_changes(self):
        self.arm()
        before = len(self.calls)
        state = self.client.get('/api/state').json()
        self.assertFalse(state['actions']['execute']['enabled'])
        reply = self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': self.service.broker_id})
        self.assertEqual(reply.status_code, 409)
        self.assertIn('自动运行', reply.json()['message'])
        self.assertEqual(self.client.post('/api/automation/config', json={'day': 2}).status_code, 409)
        self.assertEqual(len(self.calls), before)

    def test_reconcile_closes_paused_cycle_without_rearming_or_sending_orders(self):
        self.arm()
        broker = self.service.broker_id
        cycle_key = broker + ':2026-09'
        self.service.automation.data['cycles'][cycle_key] = {'broker_id': broker, 'status': 'blocked'}
        self.service.automation.pause('Offline fixture: unresolved order')
        self.response = {'status': 'complete', 'message': 'Fixture ledger is clear',
                         'recovery_state': 'complete'}
        reply = self.client.post('/api/jobs', json={'action': 'reconcile'})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.finish()
        self.assertEqual(self.service.automation.data['cycles'][cycle_key]['status'], 'closed')
        self.assertFalse(self.service.automation.data['enabled'])
        self.assertFalse(self.client.get('/api/state').json()['automation']['enabled'])
        self.assertEqual([args[1] for args, _ in self.calls], ['status', 'reconcile'])

    def test_application_lifespan_starts_and_stops_idle_scheduler(self):
        self.assertIsNone(self.service.scheduler_thread)
        app = create_app(self.root, service=self.service)
        with TestClient(app, base_url='http://127.0.0.1:8765') as client:
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertTrue(self.service.scheduler_thread.is_alive())
            self.assertFalse(self.service.scheduler_stop.is_set())
            self.assertEqual(self.calls, [])
        self.assertTrue(self.service.scheduler_stop.is_set())
        self.assertFalse(self.service.scheduler_thread.is_alive())
        self.assertEqual(self.calls, [])

    def test_remainder_resolution_requires_exact_account_and_disabled_plan(self):
        for confirmation in (None, 'some-other-broker'):
            reply=self.client.post('/api/jobs',json={'action':'resolve-remainder','confirmation':confirmation})
            self.assertEqual(reply.status_code,409)
        self.assertEqual(self.calls,[])
        self.arm()
        reply=self.client.post('/api/jobs',json={'action':'resolve-remainder','confirmation':self.service.broker_id})
        self.assertEqual(reply.status_code,409)
        self.assertNotIn('resolve-remainder',[args[1] for args,_ in self.calls])

    def test_remainder_resolution_closes_cycle_without_rearming(self):
        self.arm()
        broker=self.service.broker_id
        self.service.automation.data['cycles'][broker+':2026-09']={'broker_id':broker,'status':'blocked'}
        self.service.automation.disable()
        self.response={'status':'complete','message':'Unsubmitted remainder abandoned',
                       'recovery_state':'complete','details':{'abandoned':3}}
        reply=self.client.post('/api/jobs',json={'action':'resolve-remainder','confirmation':broker})
        self.assertEqual(reply.status_code,200,reply.text)
        self.finish()
        self.assertFalse(self.service.automation.data['enabled'])
        self.assertEqual(self.service.automation.data['cycles'][broker+':2026-09']['status'],'closed')
        self.assertEqual([args[1] for args,_ in self.calls],['status','resolve-remainder'])

    def test_terminal_closure_requires_exact_account_and_disabled_plan(self):
        for confirmation in (None, 'some-other-broker'):
            reply=self.client.post('/api/jobs',json={'action':'close-terminal','confirmation':confirmation})
            self.assertEqual(reply.status_code,409)
        self.assertEqual(self.calls,[])
        self.arm()
        reply=self.client.post('/api/jobs',json={'action':'close-terminal','confirmation':self.service.broker_id})
        self.assertEqual(reply.status_code,409)
        self.assertNotIn('close-terminal',[args[1] for args,_ in self.calls])

    def test_terminal_closure_closes_cycle_without_rearming(self):
        self.arm()
        broker=self.service.broker_id
        self.service.automation.data['cycles'][broker+':2026-09']={'broker_id':broker,'status':'blocked'}
        self.service.automation.disable()
        self.response={'status':'complete','message':'Terminal orders closed',
                       'recovery_state':'complete','details':{'closed':1}}
        reply=self.client.post('/api/jobs',json={'action':'close-terminal','confirmation':broker})
        self.assertEqual(reply.status_code,200,reply.text)
        self.finish()
        self.assertFalse(self.service.automation.data['enabled'])
        self.assertEqual(self.service.automation.data['cycles'][broker+':2026-09']['status'],'closed')
        self.assertEqual([args[1] for args,_ in self.calls],['status','close-terminal'])


if __name__ == '__main__':
    unittest.main()
