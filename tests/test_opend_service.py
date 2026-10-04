"""OpenD supervision accepts remembered login only and cannot expose listeners."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.server import opend_service as service


class OpenDServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gateway = self.root / 'gateway'
        self.gateway.mkdir(mode=0o700)
        (self.gateway / 'OpenD').write_bytes(b'fixture')
        (self.gateway / 'OpenD').chmod(0o700)
        self.xml = self.gateway / 'OpenD.xml'
        self.xml.write_text('<moomoo_opend><ip>127.0.0.1</ip></moomoo_opend>')
        self.xml.chmod(0o600)
        self.settings = self.gateway / 'opend-service.json'
        self.settings.write_text(json.dumps({'provider': 'moomoo', 'login_account': 'private-test-id', 'remembered_login_confirmed': True}))
        self.settings.chmod(0o600)
        (self.root / 'deploy/server').mkdir(parents=True)
        (self.root / 'deploy/server/opend_service.py').touch()
        (self.root / '.venv/bin').mkdir(parents=True)
        (self.root / '.venv/bin/python').touch()
        self.paths = patch.object(service, 'service_path', side_effect=lambda value: Path(value).absolute())
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_service_has_private_output_throttled_restart_no_secret_and_no_autounlock(self):
        unit = service.render(self.root, self.gateway, 'moomoo')
        for value in ('--port 11112', 'StartLimitBurst=3', 'RestartSec=30', 'StandardInput=null', 'UMask=0077', 'RestartPreventExitStatus=78'):
            self.assertIn(value, unit)
        self.assertNotIn('private-test-id', unit)
        _, args, _ = service.prepare_command(self.gateway, 'moomoo')
        self.assertIn('-api_ip=127.0.0.1', args)
        self.assertIn('-login_by_remember=1', args)
        self.assertIn('-auto_hold_quote_right=0', args)
        self.assertFalse(any('pwd' in arg or 'unlock' in arg for arg in args))

    def test_gateway_requires_correct_brand_and_confirmed_remembered_login(self):
        with self.assertRaises(ValueError):
            service.validate_opend(self.gateway, 'futu')
        self.settings.write_text(json.dumps({'provider': 'moomoo', 'login_account': 'id', 'remembered_login_confirmed': False}))
        with self.assertRaises(ValueError):
            service.validate_opend(self.gateway, 'moomoo')

    def test_public_listener_password_extra_listener_and_duplicate_xml_are_rejected(self):
        for content in ('<ip>0.0.0.0</ip>', '<telnet_port>22222</telnet_port>', '<websocket_port>33333</websocket_port>',
                        '<login_pwd>secret</login_pwd>', '<ip>127.0.0.1</ip><ip>0.0.0.0</ip>'):
            self.xml.write_text('<config>' + content + '</config>')
            with self.assertRaises(ValueError):
                service.validate_opend(self.gateway, 'moomoo')

    def test_passwords_and_command_injection_cannot_enter_service_settings(self):
        for value in ({'provider': 'moomoo', 'password': 'secret'},
                      {'provider': 'moomoo', 'login_account': 'id\n-unlock=1', 'remembered_login_confirmed': True}):
            self.settings.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                service.validate_opend(self.gateway, 'moomoo')

    def test_systemd_path_expansion_and_newline_are_rejected(self):
        self.paths.stop()
        try:
            for value in ('/home/user/%n', '/home/user/$secret', '/home/user/a\nb', '/home/user/a b', '/home/user/a;cmd'):
                with self.assertRaises(ValueError):
                    service.service_path(value)
        finally:
            self.paths.start()

    @unittest.skipUnless(os.name == 'posix', 'Unix permissions')
    def test_public_private_settings_are_rejected(self):
        self.settings.chmod(0o644)
        with self.assertRaises(ValueError):
            service.validate_opend(self.gateway, 'moomoo')


if __name__ == '__main__':
    unittest.main()
