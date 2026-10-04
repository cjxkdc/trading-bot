"""Official OAuth protocol and simulation boundaries exercised with fixture transport."""
import base64
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app.control_contracts import ControlError
from app.module_registry import ModuleRegistry
from brokers.moomoo_oauth import MoomooAuthorization, request
from brokers.oauth_vault import ActiveVault
from portal.broker_session import BrokerSession
import test_portal as portal_fixture


class FakeMoomoo:
    def __init__(self):
        self.calls = []
        self.scopes = 'trade:read accid:*'
        self.positions = []
        self.orders = []
        self.accounts = [{'account_id': '1234567', 'market_id': 2, 'account_title': 'US Sandbox'},
                         {'account_id': '7654321', 'market_id': 1, 'account_title': 'HK Sandbox'}]

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if path == '/oauth2/register':
            return {'client_id': 'fixture-public-client'}
        if path == '/oauth2/token':
            return {'access_token': 'fixture-access-token', 'refresh_token': 'fixture-refresh-token',
                    'expires_in': 300, 'scope': self.scopes, 'token_type': 'Bearer'}
        if path.endswith('/accounts'):
            value = {'accounts': self.accounts}
        elif path.endswith('/cash-info'):
            value = {'balance': '1000000', 'hold': '100'}
        elif path.endswith('/positions'):
            value = {'positions': self.positions}
        elif path.endswith('/orders'):
            value = {'orders': self.orders}
        else:
            raise AssertionError('Unexpected endpoint: ' + path)
        return {'ret_code': 0, 'data': deepcopy(value)}


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.transport = FakeMoomoo()
        self.stamp = [1000]
        self.auth = MoomooAuthorization(self.root, transport=self.transport, clock=lambda: self.stamp[0])

    def tearDown(self):
        self.temp.cleanup()

    def authorize(self):
        url = self.auth.begin('http://127.0.0.1:5440/api/broker-auth/callback')['authorization_url']
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.auth.finish(state, 'fixture-code')
        return self.auth.public()

    def bind(self):
        value = self.authorize()
        self.auth.bind(value['accounts'][0]['id'])

    def control(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'brokers/plugins/moomoo_oauth_paper.json').read_text(encoding='utf-8'))
        from brokers.moomoo_oauth import MoomooOAuthControl
        return MoomooOAuthControl(self.root, {'state_dir': 'runs/moomoo-oauth-paper'}, manifest)

    def test_pkce_state_and_callback_are_bound_to_one_authorization(self):
        url = self.auth.begin('https://bot.example/api/broker-auth/callback')['authorization_url']
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).netloc, 'webapi.moomoo.com')
        self.assertEqual(query['scope'], ['trade:read'])
        expected = base64.urlsafe_b64encode(sha256(self.auth.pending['verifier'].encode()).digest()).rstrip(b'=').decode()
        verifier = self.auth.pending['verifier']
        self.assertEqual(query['code_challenge'], [expected])
        self.auth.finish(query['state'][0], 'fixture-code')
        exchange = self.transport.calls[1][2]['payload']
        self.assertEqual(exchange['redirect_uri'], query['redirect_uri'][0])
        self.assertEqual(exchange['code_verifier'], verifier)
        with self.assertRaises(ControlError):
            self.auth.finish(query['state'][0], 'fixture-code')

    def test_wrong_state_and_expired_state_never_exchange_a_code(self):
        for state, expired in [('wrong', False), ('非ASCII', False), (None, True)]:
            self.auth.begin('http://localhost:5440/api/broker-auth/callback')
            if expired:
                state = self.auth.pending['state']
                self.stamp[0] += 301
            count = len(self.transport.calls)
            with self.assertRaises(ControlError):
                self.auth.finish(state, 'fixture-code')
            self.assertEqual(len(self.transport.calls), count)

    def test_callback_origin_cannot_target_remote_cleartext_or_another_path(self):
        for url in ('http://evil.example/api/broker-auth/callback', 'https://bot.example/other',
                    'https://bot.example/api/broker-auth/callback?extra=1'):
            with self.assertRaises(ControlError):
                self.auth.begin(url)
        self.assertFalse(self.transport.calls)

    def test_tokens_are_encrypted_and_full_account_ids_do_not_reach_public_state(self):
        value = self.authorize()
        public = json.dumps(value)
        self.assertNotIn('1234567', public)
        self.assertNotIn('fixture-access-token', public)
        self.assertEqual(len(value['accounts']), 1)
        self.assertEqual(value['accounts'][0]['environment'], 'SIMULATE')
        ciphertext = ActiveVault(self.root).path.read_bytes()
        self.assertNotIn(b'fixture-access-token', ciphertext)
        self.assertNotIn(b'1234567', ciphertext)

    def test_write_scope_and_missing_read_scope_are_rejected(self):
        for scopes in ('trade:read trade:write', 'quote:read'):
            self.transport.scopes = scopes
            with self.assertRaises(ControlError):
                self.authorize()
            self.assertIsNone(ActiveVault(self.root).load('moomoo_oauth_paper'))

    def test_binding_accepts_only_the_opaque_us_simulation_choice(self):
        value = self.authorize()
        for ident in ('1234567', '7654321', 'unknown'):
            with self.assertRaises(ControlError):
                self.auth.bind(ident)
        selected = value['accounts'][0]['id']
        self.assertEqual(self.auth.bind(selected)['selected'], selected)

    def test_token_refresh_preserves_selected_account_and_has_no_order_operation(self):
        self.bind()
        selected = self.auth.public()['selected']
        self.stamp[0] += 280
        self.auth.session()
        self.assertEqual(self.auth.public()['selected'], selected)
        self.assertEqual(self.transport.calls[-1][2]['payload']['grant_type'], 'refresh_token')
        self.assertFalse(any('input-order' in path for _, path, _ in self.transport.calls))

    def test_disconnect_clears_pending_and_prevents_reusing_saved_tokens(self):
        self.bind()
        self.auth.disconnect()
        self.assertFalse(self.auth.public()['authorized'])
        with self.assertRaises(ControlError):
            self.auth.session()
        self.assertFalse(ActiveVault(self.root).path.exists())

    def test_single_vault_replaces_the_previous_broker_and_detects_tampering(self):
        vault = ActiveVault(self.root)
        vault.save('a', {'access_token': 'first'})
        vault.save('b', {'access_token': 'second'})
        self.assertIsNone(vault.load('a'))
        self.assertEqual(vault.load('b')['access_token'], 'second')
        vault.path.write_bytes(b'not encrypted')
        with self.assertRaises(ControlError):
            vault.load('b')

    def test_transport_disallows_real_accounts_and_all_order_writes_before_network(self):
        for method, path in [('GET', '/api/v1.0/trade/accounts'), ('POST', '/api/v1.0/sim-trade/123/input-order'),
                             ('GET', '/api/v1.0/sim-trade/../cash-info')]:
            with self.assertRaises(ControlError):
                request(method, path)

    def test_control_only_reads_selected_sandbox_and_exposes_no_order_capability(self):
        self.bind()
        self.transport.positions = [{'symbol': 'SPY', 'qty': '3', 'qty_avbl': '2', 'pstn_type': 0, 'market': 2}]
        self.transport.orders = [{'order_id': '1', 'status': 2}]
        with patch('brokers.moomoo_oauth.MoomooAuthorization', return_value=self.auth):
            result = self.control().inspect(connect=True)
        self.assertEqual(result['account_environment'], 'SIMULATE')
        self.assertEqual(result['cash'], '999900')
        self.assertEqual(result['open_order_count'], 1)
        self.assertFalse(result['order_access'])
        self.assertTrue(all('/sim-trade/1234567/' in path for _, path, _ in self.transport.calls[-3:]))
        with self.assertRaises(ControlError):
            self.control().session()

    def test_unknown_orders_fractional_short_or_other_market_positions_block_check(self):
        self.bind()
        for row in ({'symbol': 'SPY', 'qty': '1.5', 'qty_avbl': '1', 'pstn_type': 0, 'market': 2},
                    {'symbol': 'SPY', 'qty': '1', 'qty_avbl': '1', 'pstn_type': 1, 'market': 2},
                    {'symbol': '00700', 'qty': '100', 'qty_avbl': '100', 'pstn_type': 0, 'market': 1}):
            self.transport.positions = [row]
            with patch('brokers.moomoo_oauth.MoomooAuthorization', return_value=self.auth), self.assertRaises(ControlError):
                self.control().inspect(connect=True)
        self.transport.positions = []
        self.transport.orders = [{'order_id': '1', 'status': 999}]
        with patch('brokers.moomoo_oauth.MoomooAuthorization', return_value=self.auth), self.assertRaises(ControlError):
            self.control().inspect(connect=True)


class BrokerVerificationTests(unittest.TestCase):
    setUp = portal_fixture.PortalTests.setUp
    tearDown = portal_fixture.PortalTests.tearDown
    finish = portal_fixture.PortalTests.finish

    def test_prepare_requires_current_account_check_before_any_worker(self):
        with self.assertRaisesRegex(ControlError, '尚未核验'):
            self.service.start('prepare')
        self.assertFalse(self.calls)

    def test_configuration_change_during_account_check_never_grants_new_binding(self):
        self.wait = threading.Event()
        self.service.start('status')
        path = self.root / 'secrets/futu/connection.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}', encoding='utf-8')
        self.wait.set()
        self.finish()
        self.assertFalse(self.service.state()['broker_verified'])
        self.assertIsNone(self.service.account)
        self.assertEqual(self.service.jobs[0]['status'], 'blocked')

    def test_switch_invalidates_old_verification_preview_and_oauth_state(self):
        self.service.start('status'); self.finish()
        self.assertTrue(self.service.state()['broker_verified'])
        ActiveVault(self.root).save('other', {'access_token': 'fixture'})
        self.service.preview = {'execution_ready': True}
        self.service.select_broker('moomoo_paper')
        self.assertFalse(self.service.state()['broker_verified'])
        self.assertIsNone(self.service.preview)
        self.assertFalse(ActiveVault(self.root).path.exists())

    def test_open_or_unknown_ledger_orders_block_switch_without_altering_selection(self):
        path = self.service.registry.control('broker', self.service.broker_id).journal_path
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE intents (status TEXT)')
            db.execute("INSERT INTO intents VALUES ('unknown')")
        db.close()
        with self.assertRaises(ControlError):
            self.service.select_broker('moomoo_paper')
        self.assertEqual(self.service.broker_id, 'futu_paper')
        with sqlite3.connect(path) as db:
            db.execute("UPDATE intents SET status='filled'")
        db.close()
        self.service.select_broker('moomoo_paper')

    def test_cached_open_orders_block_disconnect(self):
        self.service.account = {'status': 'connected', 'details': {'open_order_count': 1}}
        with self.assertRaises(ControlError):
            self.service.disconnect_broker()
        self.assertIsNotNone(self.service.account)

    def test_grant_expiry_restart_and_other_binding_require_new_check(self):
        stamp = [100]
        session = BrokerSession(clock=lambda: stamp[0], seconds=20)
        session.verify('a', 'binding')
        self.assertTrue(session.valid('a', 'binding'))
        self.assertFalse(session.valid('b', 'binding'))
        self.assertFalse(session.valid('a', 'different'))
        self.assertFalse(BrokerSession().valid('a', 'binding'))
        stamp[0] += 21
        with self.assertRaises(ControlError):
            session.require('a', 'binding')

    def test_denied_callback_consumes_pending_request_and_returns_to_portal(self):
        self.service.select_broker('moomoo_oauth_paper')
        auth = MoomooAuthorization(self.root, transport=FakeMoomoo())
        self.service.authorization = auth
        auth.begin('http://127.0.0.1:8765/api/broker-auth/callback')
        state = auth.pending['state']
        reply = self.client.get('/api/broker-auth/callback?state=' + state + '&error=private-vendor-error',
                                headers={'Sec-Fetch-Site': 'cross-site'}, follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertEqual(reply.headers['location'], '/')
        self.assertIsNone(auth.pending)
        self.assertFalse(auth.public()['authorized'])
        self.assertNotIn('private-vendor-error', json.dumps(self.service.state()))

    def test_switched_broker_does_not_accept_the_previous_authorization_callback(self):
        self.service.select_broker('moomoo_oauth_paper')
        auth = MoomooAuthorization(self.root, transport=FakeMoomoo())
        self.service.authorization = auth
        auth.begin('http://127.0.0.1:8765/api/broker-auth/callback')
        state = auth.pending['state']
        self.service.select_broker('moomoo_paper')
        reply = self.client.get('/api/broker-auth/callback?state=' + state + '&code=private-code', follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertFalse(ActiveVault(self.root).path.exists())
        self.assertFalse(self.calls)
