"""Release archives cannot carry credentials, execution ledgers or virtual environments."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from deploy.server.render_service import render
from tools.package_release import package


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'project'
        self.root.mkdir()
        files = {'tests/fixtures/strategy_samples/strategies/hold.py': 'TEST-ONLY-STRATEGY', 'README.md': 'public', 'CHANGELOG.md': 'Release history', 'PRODUCT.md': 'Product scope', 'app/main.py': 'print("public")', 'configs/portfolio_config.json': '{}',
                 'ui/dist/index.html': '<html></html>', 'deploy/server/install.sh': '#!/bin/bash\n',
                 'deploy/desktop/install.ps1': '# 中文安装\n', 'secrets/tiger/private.pem': 'PRIVATE',
                 'secrets/portal/server.json': 'PASSWORD-HASH', 'runs/paper/journal.sqlite': 'LEDGER',
                 '.venv/pyvenv.cfg': 'PRIVATE', 'ui/node_modules/extra.js': 'DEPENDENCY',
                 'configs/connection.json': 'ACCOUNT-ID', 'app/secrets/nested.json': 'TOKEN',
                 'configs/portal/cloud.json': 'PRIVATE-CLOUD-URL',
                 'configs/execution_policy.json': 'PRIVATE-LIVE-ACCOUNT-GRANT',
                 'configs/execution_policy.example.json': '{"version":1,"live_accounts":{}}'}
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        self.output = Path(self.temp.name) / 'output'

    def tearDown(self):
        self.temp.cleanup()

    def test_server_zip_is_public_source_and_prebuilt_ui_only(self):
        path = package(self.root, self.output, 'server')
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            all_content = b''.join(bundle.read(name) for name in names)
            for private in (b'PRIVATE', b'PASSWORD-HASH', b'LEDGER', b'ACCOUNT-ID', b'TOKEN', b'DEPENDENCY', b'PRIVATE-CLOUD-URL', b'TEST-ONLY-STRATEGY'):
                self.assertNotIn(private, all_content)
            self.assertNotIn('Trading-bot/configs/execution_policy.json', names)
            self.assertIn('Trading-bot/configs/execution_policy.example.json', names)
            self.assertIn('Trading-bot/ui/dist/index.html', names)
            self.assertIn('Trading-bot/deploy/server/install.sh', names)
            self.assertFalse(any(name.endswith('.exe') for name in names))
            self.assertIn('Trading-bot/START-HERE.txt', names)
            self.assertIn('Trading-bot/CHANGELOG.md', names)
            self.assertIn('Trading-bot/PRODUCT.md', names)
            self.assertIn('Trading-bot/deploy/desktop/install.ps1', names)
            self.assertEqual(bundle.getinfo('Trading-bot/deploy/server/install.sh').external_attr >> 16, 0o100755)

    def test_variants_share_identical_application_code_and_have_hash_manifests(self):
        archives = [package(self.root, self.output, variant) for variant in ('server', 'desktop')]
        records = []
        for path in archives:
            with zipfile.ZipFile(path) as bundle:
                record = json.loads(bundle.read('Trading-bot/release-manifest.json'))
                self.assertFalse(record['credentials_included'])
                self.assertEqual(record['execution_policy'], 'account_scoped')
                for name, digest in record['sha256'].items():
                    self.assertEqual(sha256(bundle.read('Trading-bot/' + name)).hexdigest(), digest)
                records.append(record)
        for name in ('app/main.py', 'configs/portfolio_config.json', 'ui/dist/index.html'):
            self.assertEqual(records[0]['sha256'][name], records[1]['sha256'][name])
        with zipfile.ZipFile(archives[1]) as bundle:
            self.assertTrue(bundle.read('Trading-bot/deploy/desktop/install.ps1').startswith(b'\xef\xbb\xbf'))

    def test_absent_frontend_and_unknown_variant_cannot_produce_ready_release(self):
        with self.assertRaises(ValueError):
            package(self.root, self.output, 'unknown')
        (self.root / 'ui/dist/index.html').unlink()
        with self.assertRaises(ValueError):
            package(self.root, self.output, 'server')

    def test_service_uses_authenticated_private_port_and_graceful_process_stop(self):
        project = MagicMock()
        project.__str__.return_value = '/home/ubuntu/trading-bot'
        project.__truediv__.return_value.is_file.return_value = True
        with patch('deploy.server.render_service.Path') as path:
            path.return_value.resolve.return_value = project
            unit = render('fixture')
        self.assertIn('python -m app.portal_server --server --port 5329', unit)
        self.assertIn('KillMode=process', unit)
        self.assertIn('TimeoutStopSec=infinity', unit)
        self.assertNotIn('0.0.0.0', unit)
        self.assertNotIn('password', unit)
