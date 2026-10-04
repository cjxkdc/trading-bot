"""Remote owner access controls, with no server creation or broker traffic."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from app.control_contracts import ControlError
from app.origins import validate_server_origin
from portal.access import COOKIE, OwnerSessions, RemoteSettings, password_record, validate_origin
from portal.controller import create_controller
from portal.runtime import Reply


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.running = True

    def status(self):
        return {'status': 'running' if self.running else 'stopped'}

    def start(self):
        self.running = True
        return self.status()

    def stop(self, **kwargs):
        self.running = False
        return self.status()

    def forward(self, method, path, content=None, content_type=None):
        self.calls.append((method, path, content, content_type))
        if path.startswith('/api/broker-auth/callback'):
            return Reply(303, b'', {'location': '/'})
        return Reply(200, b'{"message":"fixture"}', {'content-type': 'application/json'})


class AccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'Test password 2026!'
        cls.settings = RemoteSettings('https://bot.example', password_record(cls.password))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = FakeRuntime()
        self.app = create_controller(self.temp.name, port=5329, instance='test',
                                     runtime=self.runtime, access=self.settings)
        self.client = TestClient(self.app, base_url=self.settings.origin)
        self.token = self.client.get('/api/auth/status').json()['login_token']
        self.client.headers.update({'Origin': self.settings.origin, 'X-Portal-Token': self.token})

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self):
        return self.client.post('/api/auth/login', json={'username': 'owner', 'password': self.password})

    def test_locked_portal_cannot_read_state_session_or_control_engine(self):
        for path in ('state', 'session', 'runtime', 'orders'):
            self.assertEqual(self.client.get('/api/' + path).status_code, 401)
        self.assertEqual(self.client.post('/api/runtime/stop', json={}).status_code, 401)
        self.assertTrue(self.runtime.running)
        self.assertFalse(self.runtime.calls)

    def test_password_cookie_login_and_logout_do_not_stop_engine(self):
        reply = self.login()
        self.assertEqual(reply.status_code, 200)
        cookie = reply.headers['set-cookie'].lower()
        for value in ('httponly', 'secure', 'samesite=strict'):
            self.assertIn(value, cookie)
        self.assertNotIn(self.password, reply.text)
        self.assertTrue(self.client.get('/api/auth/status').json()['authenticated'])
        self.assertEqual(self.client.get('/api/runtime').status_code, 200)
        self.assertEqual(self.client.post('/api/auth/logout', json={}).status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        self.assertTrue(self.runtime.running)

    def test_login_requires_same_origin_csrf_token_and_exact_host(self):
        for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                        {'Origin': ''}, {'X-Portal-Token': ''}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.client.post('/api/auth/login', headers=headers,
                json={'username': 'owner', 'password': self.password}).status_code, 403)

    def test_duplicate_origin_and_host_are_rejected(self):
        self.assertEqual(self.client.get('/api/auth/status', headers=[
            ('Host', 'bot.example'), ('Host', 'evil.example')]).status_code, 403)

    def test_cookie_alone_cannot_submit_commands(self):
        self.login()
        for headers in ({'Origin': ''}, {'X-Portal-Token': ''}):
            self.assertEqual(self.client.post('/api/runtime/stop', headers=headers, json={}).status_code, 403)
        self.assertTrue(self.runtime.running)

    def test_login_payload_bound_and_error_never_echoes_credentials(self):
        secret = 'x' * 5000
        reply = self.client.post('/api/auth/login', json={'username': 'owner', 'password': secret})
        self.assertEqual(reply.status_code, 413)
        self.assertNotIn(secret, reply.text)
        reply = self.client.post('/api/auth/login', json={'username': 'wrong', 'password': 'private-not-a-password'})
        self.assertEqual(reply.status_code, 409)
        self.assertNotIn('private-not-a-password', reply.text)

    def test_authenticated_proxy_and_callback_use_trusted_origin(self):
        self.login()
        self.assertEqual(self.client.post('/api/broker-auth/start', json={}).status_code, 200)
        self.assertEqual(json.loads(self.runtime.calls[-1][2]), {'callback_origin': self.settings.origin})
        self.assertEqual(self.client.post('/api/broker-auth/start', json={
            'callback_origin': 'https://evil.example'}).status_code, 409)
        self.assertNotIn(self.password.encode(), self.runtime.calls[-1][2])
        self.client.cookies.clear()
        reply = self.client.get('/api/broker-auth/callback?state=test&code=test',
            headers={'Sec-Fetch-Site': 'cross-site', 'Origin': ''}, follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertEqual(reply.headers['location'], '/')

    def test_owner_cannot_shutdown_the_remote_control_portal(self):
        self.login()
        self.assertEqual(self.client.post('/api/shutdown', json={}).status_code, 409)
        self.assertTrue(self.runtime.running)

    def test_secret_and_account_files_are_not_static_content(self):
        self.login()
        for path in ('/secrets/portal/server.json', '/configs/portfolio_config.json', '/runs/portal/preferences.json'):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_password_hash_and_session_expiry_are_independent_of_brokers(self):
        stamp = [100]
        owner = OwnerSessions(self.settings, clock=lambda: stamp[0])
        cookie = owner.login('owner', self.password)
        self.assertTrue(owner.authenticated(cookie))
        self.assertFalse(OwnerSessions(self.settings).authenticated(cookie))
        stamp[0] += self.settings.session_seconds + 1
        self.assertFalse(owner.authenticated(cookie))
        self.assertNotIn(self.password, json.dumps(self.settings.password))

    def test_global_rate_limit_cannot_be_bypassed_by_forwarded_ip(self):
        owner = OwnerSessions(self.settings, clock=lambda: 100)
        owner.attempts.extend([99] * 8)
        with self.assertRaisesRegex(ControlError, '过多'):
            owner.login('owner', self.password)

    def test_remote_settings_require_https_while_desktop_callback_can_use_loopback(self):
        for origin in ('http://aws.example', 'https://bot.example/extra', 'https://user:pass@bot.example', 'file:///tmp'):
            with self.assertRaises(ControlError):
                validate_origin(origin)
        self.assertEqual(validate_origin('http://127.0.0.1:5329'), 'http://127.0.0.1:5329')
        with self.assertRaises(ControlError):
            validate_server_origin('http://127.0.0.1:5329')
        with self.assertRaises(ControlError):
            RemoteSettings('http://127.0.0.1:5329', self.settings.password)
        root = Path(self.temp.name)
        path = root / 'secrets/portal/server.json'
        path.parent.mkdir(parents=True)
        with self.assertRaises(ControlError):
            RemoteSettings.load(root)
        data = {'origin': self.settings.origin, 'username': 'owner', 'password': deepcopy(self.settings.password),
                'session_seconds': 3600}
        path.write_text(json.dumps(data), encoding='utf-8')
        self.assertEqual(RemoteSettings.load(root).session_seconds, 3600)
        data['origin'] = 'http://127.0.0.1:5329'
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ControlError):
            RemoteSettings.load(root)
        data['origin'] = self.settings.origin
        data['password']['iterations'] = 1
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ControlError):
            RemoteSettings.load(root)
