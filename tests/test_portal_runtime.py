"""Process lifecycle and controller tests; isolated files, no vendor requests."""
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from app.control_contracts import ControlError
from data.snapshot import demo_snapshot
from portal.app import create_app
from portal.controller import create_controller
from portal.runtime import BackendRuntime, Reply
from portal.service import PortalService

ROOT = Path(__file__).resolve().parents[1]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portal-runtime-')
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'configs', self.root / 'configs')
        for folder in ('strategies', 'data', 'brokers'):
            shutil.copytree(ROOT / folder / 'plugins', self.root / folder / 'plugins')
        static = self.root / 'ui/dist'
        static.mkdir(parents=True)
        (static / 'index.html').write_text('test portal remains available', encoding='utf-8')
        self.runtime = BackendRuntime(self.root)
        self.client = TestClient(create_controller(self.root, port=8765, instance='test-parent', runtime=self.runtime),
                                 base_url='http://127.0.0.1:8765')
        self.token = self.client.get('/api/session').json()['token']
        self.client.headers['X-Portal-Token'] = self.token

    def tearDown(self):
        self.runtime.close()
        self.client.close()
        self.temp.cleanup()

    def wait_status(self, expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.client.get('/api/runtime').json()
            if value['status'] == expected:
                return value
            self.assertNotEqual(value['status'], 'failed', value)
            time.sleep(.04)
        self.fail(f'Backend did not become {expected}: {value}')

    def test_real_stop_restart_keeps_portal_and_never_replays_jobs(self):
        self.assertEqual(self.client.get('/api/runtime').json()['status'], 'stopped')
        self.assertEqual(self.client.get('/api/state').status_code, 409)
        self.assertEqual(self.client.post('/api/runtime/start', json={}).status_code, 200)
        started = self.wait_status('running')
        self.assertTrue(started['alive'])
        self.assertIsNotNone(started['pid'])
        self.assertNotEqual(self.token, self.runtime.info['token'])
        self.assertNotIn(self.runtime.info['token'], self.client.get('/api/runtime').text)
        first_process = self.runtime.process
        self.client.post('/api/runtime/start', json={})
        self.assertIs(first_process, self.runtime.process)
        self.assertIsNone(self.client.get('/api/state').json()['account'])

        # A preview uses only a synthetic file and the local paper module.
        self.assertEqual(self.client.post('/api/broker', json={'broker': 'paper'}).status_code, 200)
        self.assertEqual(self.client.post('/api/source', json={'source': 'snapshot_file'}).status_code, 200)
        self.assertEqual(self.client.post('/api/snapshot', content=json.dumps(demo_snapshot()).encode(),
                                         headers={'Content-Type': 'application/octet-stream'}).status_code, 200)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'preview'}).status_code, 200)
        deadline = time.monotonic() + 8
        # This fixture has no .venv worker executable, so the child must fail closed.
        while time.monotonic() < deadline:
            state = self.client.get('/api/state').json()
            if not state['active']:
                break
            time.sleep(.03)
        self.assertFalse(state['active'])
        self.assertEqual(state['jobs'][0]['status'], 'blocked')
        old_job = state['jobs'][0]['id']

        self.assertEqual(self.client.post('/api/runtime/stop', json={}).status_code, 200)
        self.wait_status('stopped')
        self.assertEqual(self.client.get('/api/runtime').json()['status'], 'stopped')
        self.assertFalse(first_process.is_alive())
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertIn('test portal remains available', self.client.get('/').text)
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute'}).status_code, 409)
        self.assertFalse(json.loads(self.runtime.preferences.read_text(encoding='utf-8'))['enabled'])
        self.assertEqual(self.client.post('/api/runtime/start', json={}).status_code, 200)
        restarted = self.wait_status('running')
        self.assertNotEqual(started['pid'], restarted['pid'])
        state = self.client.get('/api/state').json()
        self.assertIsNone(state['preview'])
        self.assertIsNone(state['account'])
        self.assertEqual(state['jobs'][0]['id'], old_job)
        self.assertEqual(len(state['jobs']), 1)
        self.assertEqual(state['broker']['id'], 'paper')
        self.assertEqual(state['source'], 'snapshot_file')
        self.assertEqual(self.client.post('/api/jobs', json={'action': 'execute', 'confirmation': 'paper'}).status_code, 409)
        self.assertFalse((self.root / 'runs/portal-local-paper').exists())
        self.assertFalse((self.root / 'runs/futu-paper').exists())

    def test_switch_endpoints_require_local_session_and_empty_intent(self):
        for path in ('runtime/start', 'runtime/stop'):
            for headers in ({'X-Portal-Token': ''}, {'Origin': 'https://evil.example'}, {'Host': 'evil.example'}):
                self.assertEqual(self.client.post('/api/' + path, json={}, headers=headers).status_code, 403)
            self.assertEqual(self.client.post('/api/' + path, json={'action': 'execute'}).status_code, 409)
        self.assertEqual(self.client.get('/api/session/').status_code, 404)
        self.assertIsNone(self.runtime.process)

    def test_disabled_preference_does_not_start_on_next_launch(self):
        self.runtime.stop()
        restored = BackendRuntime(self.root)
        restored.autostart()
        self.assertEqual(restored.status()['status'], 'stopped')
        self.assertIsNone(restored.process)
        restored.close()

    def fake_process(self, alive=True):
        process = Mock()
        process.pid = 12345
        process.is_alive.return_value = alive
        self.runtime.process = process
        self.runtime.phase = 'running'
        self.runtime.info = {'port': 9999, 'instance': 'test-child', 'token': 'private-child-token'}
        return process

    def test_busy_backend_rejects_shutdown_without_forcing_or_losing_state(self):
        process = self.fake_process()
        self.runtime.started_at = '2026-10-02T00:00:00+00:00'
        with patch.object(self.runtime, '_request', return_value=Reply(409, b'{}', {})):
            self.assertEqual(self.client.post('/api/runtime/stop', json={}).status_code, 409)
        self.assertEqual(self.runtime.phase, 'running')
        self.assertEqual(self.runtime.started_at, '2026-10-02T00:00:00+00:00')
        self.assertFalse(self.runtime.preferences.exists())
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_unresponsive_is_not_reported_stopped_or_automatically_restarted(self):
        process = self.fake_process()
        with patch.object(self.runtime, '_request', side_effect=TimeoutError):
            self.assertEqual(self.runtime.status()['status'], 'unresponsive')
            self.runtime.start()
            self.assertIs(self.runtime.process, process)
            with self.assertRaises(ControlError):
                self.runtime.stop()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_unexpected_exit_is_stable_and_does_not_restart(self):
        self.fake_process(alive=False)
        with patch.object(self.runtime.context, 'Process') as spawn:
            self.assertEqual(self.runtime.status()['status'], 'failed')
            self.assertEqual(self.runtime.status()['status'], 'failed')
            spawn.assert_not_called()
        self.assertIsNone(self.runtime.info)

    def test_wrong_health_instance_cannot_look_like_running_backend(self):
        self.fake_process()
        with patch.object(self.runtime, '_request', return_value=Reply(200,
                b'{"application":"trading-bot-portal","instance":"wrong"}', {})):
            self.assertEqual(self.runtime.status()['status'], 'unresponsive')

    def test_proxy_preserves_failure_and_never_retries_a_mutation(self):
        self.fake_process()
        with patch.object(self.runtime, '_request', side_effect=TimeoutError) as request:
            response = self.client.post('/api/jobs', json={'action': 'execute'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(request.call_count, 1)

    def test_backend_shutdown_route_refuses_an_active_job(self):
        done = threading.Event()
        def worker(*args, **kwargs):
            done.wait(3)
            return {'status': 'ready', 'message': 'test only'}
        service = PortalService(self.root, worker=worker)
        shutdown = Mock()
        client = TestClient(create_app(self.root, service=service, shutdown=shutdown), base_url='http://127.0.0.1:8765')
        client.headers['X-Portal-Token'] = client.get('/api/session').json()['token']
        service.start('offline')
        try:
            self.assertEqual(client.post('/api/shutdown', json={}).status_code, 409)
            self.assertFalse(service.closing)
            shutdown.assert_not_called()
        finally:
            done.set()
            service.thread.join(4)
        self.assertEqual(client.post('/api/shutdown', json={}).status_code, 200)
        shutdown.assert_called_once()
        self.assertTrue(service.closing)
        client.close()


if __name__ == '__main__':
    unittest.main()
