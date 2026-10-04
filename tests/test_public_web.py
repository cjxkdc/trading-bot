"""HTTPS publication preserves login credentials and excludes private files."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from app.control_contracts import ControlError
from deploy.server.publish_origin import migrate
from deploy.server.web_front import public_host, render, renewal_service, renewal_timer
from portal.access import ITERATIONS


class PublicWebTests(unittest.TestCase):
    def test_gateway_rejects_injection_and_private_addresses(self):
        for origin in ('http://8.8.8.8', 'https://127.0.0.1', 'https://10.0.0.1',
                       'https://u:p@bot.example.com', 'https://bot.example.com/path',
                       'https://bot.example.com:5329', 'https://bad;host.com',
                       'https://bot.example.com?token=secret', 'https://bot.local'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                public_host(origin)
        self.assertEqual(public_host('https://8.8.8.8'), '8.8.8.8')

    def test_plain_http_only_challenge_and_redirect_and_tls_proxy_keeps_host(self):
        http = render('https://8.8.8.8')
        self.assertNotIn('proxy_pass', http)
        self.assertIn('return 308 https://8.8.8.8$request_uri', http)
        tls = render('https://8.8.8.8', tls=True)
        self.assertIn('proxy_pass http://127.0.0.1:5329', tls)
        self.assertIn('proxy_set_header Host $http_host;', tls)
        self.assertNotIn('root /home', tls)
        self.assertIn('access_log off;', tls)
        self.assertIn('ssl_protocols TLSv1.2 TLSv1.3', tls)
        self.assertIn('--deploy-hook /usr/local/sbin/trading-bot-web-reload', renewal_service())
        self.assertIn('Persistent=true', renewal_timer())

    def test_url_migration_preserves_password_and_account_state_and_requires_stopped_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'secrets/portal/server.json'
            path.parent.mkdir(parents=True)
            old = {'origin': 'http://127.0.0.1:5440', 'username': 'owner', 'session_seconds': 28800,
                   'password': {'algorithm': 'pbkdf2_sha256', 'iterations': ITERATIONS,
                                'salt': 'a' * 32, 'digest': 'b' * 64}}
            path.write_text(json.dumps(old))
            account = root / 'secrets/broker.json'
            account.write_text('PRIVATE')
            result = migrate(root, 'https://8.8.8.8')
            self.assertEqual(result['status'], 'validated')
            self.assertEqual(json.loads(path.read_text()), old)
            def running():
                raise ValueError('active service')
            with self.assertRaises(ValueError):
                migrate(root, 'https://8.8.8.8', apply=True, stopped_check=running)
            self.assertEqual(json.loads(path.read_text()), old)
            migrate(root, 'https://8.8.8.8', apply=True, stopped_check=lambda: None)
            new = json.loads(path.read_text())
            expected = deepcopy(old)
            expected['origin'] = 'https://8.8.8.8'
            self.assertEqual(new, expected)
            self.assertEqual(account.read_text(), 'PRIVATE')
            self.assertEqual(json.loads((root / 'runs/upgrades/public-web-origin.json').read_text()), old)
            with self.assertRaises(ControlError):
                migrate(root, 'http://127.0.0.1:5329', apply=True, stopped_check=lambda: None)


if __name__ == '__main__':
    unittest.main()
