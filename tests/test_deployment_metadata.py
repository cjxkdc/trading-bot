"""Deployment metadata is labels and official links, never a command transport."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from app.deployment_metadata import deployment_links, load_deployment, metadata_path, validate_deployment
from deploy.server.configure_deployment import configure


class DeploymentMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.value = {'provider': 'aws_lightsail', 'region': 'ap-southeast-2', 'instance_name': 'test-bot'}
        self.terminal = 'https://123456789012-example.ap-southeast-2.console.aws.amazon.com/lightsail/remote/ap-southeast-2/instances/test-bot/terminal?protocol=ssh'

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_or_invalid_metadata_stays_unknown_without_fabricated_terminal(self):
        self.assertEqual(load_deployment(self.root), {'provider': None, 'region': None, 'instance_name': None})
        self.assertEqual(deployment_links(load_deployment(self.root)), {'console': None, 'terminal': None})
        self.assertEqual(deployment_links(self.value), {'console': 'https://lightsail.aws.amazon.com/', 'terminal': None})
        path = configure(self.root, self.value)
        path.write_text('invalid JSON')
        self.assertIsNone(load_deployment(self.root)['provider'])

    def test_confirmed_aws_terminal_url_retained_without_normalization_or_query_expansion(self):
        raw = dict(self.value, terminal_url=self.terminal)
        self.assertEqual(validate_deployment(raw), raw)
        path = configure(self.root, raw)
        self.assertEqual(load_deployment(self.root), raw)
        self.assertEqual(deployment_links(raw)['terminal'], self.terminal)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_redirect_credentials_cross_instance_non_aws_and_command_urls_rejected(self):
        for url in [self.terminal.replace('https:', 'http:'), self.terminal.replace('.amazon.com', '.amazon.com.evil.invalid'),
                    self.terminal.replace('https://', 'https://user:password@'), self.terminal.replace('.com/', '.com:443/'),
                    self.terminal + '&command=reboot', self.terminal + '&token=PRIVATE', self.terminal + '#private',
                    self.terminal.replace('/test-bot/', '/other-bot/'), self.terminal.replace('ap-southeast-2', 'us-east-1'),
                    self.terminal.replace('/instances/', '/%2e%2e/'), '\n' + self.terminal,
                    'javascript:alert(1)', 'https://lightsail.aws.amazon.com/ls/webapp/ap-southeast-2/instances/test-bot/connect']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_deployment(dict(self.value, terminal_url=url))
        self.assertIsNone(deployment_links(dict(self.value, terminal_url='javascript:alert(1)'))['terminal'])

    def test_fields_are_allowlisted_no_credentials_or_shell_values(self):
        for update in ({'provider': 'custom'}, {'region': 'ap-southeast-2;cmd'}, {'instance_name': '../../secret'},
                       {'instance_name': 'bot\ncmd'}, {'api_key': 'secret'}, {'terminal_command': 'reboot'}, {'region': 123}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_deployment(dict(self.value, **update))

    def test_setup_only_writes_metadata_and_preserves_auth_bytes(self):
        auth = self.root / 'secrets/portal/server.json'
        auth.parent.mkdir(parents=True)
        auth.write_bytes(b'KEEP AUTH EXACTLY')
        path = configure(self.root, self.value)
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            configure(self.root, dict(self.value, instance_name='new-name'))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(auth.read_bytes(), b'KEEP AUTH EXACTLY')
        configure(self.root, dict(self.value, instance_name='new-name'), replace=True)
        self.assertEqual(load_deployment(self.root)['instance_name'], 'new-name')
        self.assertEqual(auth.read_bytes(), b'KEEP AUTH EXACTLY')

    def test_atomic_write_failure_leaves_existing_metadata_and_no_temporary_file(self):
        path = configure(self.root, self.value)
        original = path.read_bytes()
        with patch('deploy.server.configure_deployment.os.replace', side_effect=OSError('write failed')), self.assertRaises(OSError):
            configure(self.root, dict(self.value, instance_name='new-name'), replace=True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob('.deployment-*')), [])

    def test_linked_metadata_path_is_rejected(self):
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path.name == 'portal'):
            with self.assertRaises(ValueError):
                metadata_path(self.root)
            self.assertIsNone(load_deployment(self.root)['provider'])

    @unittest.skipUnless(os.name == 'posix', 'Unix private-file permissions')
    def test_world_readable_metadata_not_loaded(self):
        path = configure(self.root, self.value)
        path.chmod(0o644)
        self.assertIsNone(load_deployment(self.root)['provider'])


if __name__ == '__main__':
    unittest.main()
