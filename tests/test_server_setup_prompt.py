"""Protect the existing browser-SSH password prompt repair during upgrades."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

from app import server_setup


class ServerSetupPromptTests(unittest.TestCase):
    def test_short_and_mismatched_passwords_retry_without_printing_passwords(self):
        valid = 'private-test-password-012345'
        inputs = ['short', 'first-test-password-012345', 'wrong-test-password-012345', valid, valid]
        output = StringIO()
        record = {'digest': 'fixture-hash'}
        with patch.object(server_setup, 'getpass', side_effect=inputs) as prompt, patch.object(server_setup, 'password_record', return_value=record) as encode, redirect_stdout(output):
            self.assertIs(server_setup.prompt_password(), record)
        self.assertEqual(prompt.call_count, 5)
        encode.assert_called_once_with(valid)
        self.assertIn('Password must be 12-256 characters', output.getvalue())
        self.assertIn('Passwords did not match', output.getvalue())
        for value in inputs:
            self.assertNotIn(value, output.getvalue())
        self.assertTrue(output.getvalue().isascii())

    def test_valid_password_is_confirmed_and_only_the_record_is_returned(self):
        password = 'another-private-password'
        record = {'digest': 'fixture-hash'}
        output = StringIO()
        with patch.object(server_setup, 'getpass', side_effect=[password, password]), patch.object(server_setup, 'password_record', return_value=record), redirect_stdout(output):
            self.assertEqual(server_setup.prompt_password(), record)
        self.assertNotIn(password, output.getvalue())

    def test_cancel_never_writes_configuration(self):
        for interruption in (KeyboardInterrupt, EOFError):
            with self.subTest(interruption=interruption.__name__):
                output, error = StringIO(), StringIO()
                with patch('sys.argv', ['server_setup', '--origin', 'https://bot.example']), patch.object(server_setup, 'getpass', side_effect=interruption), patch.object(server_setup, 'atomic_json') as write, redirect_stdout(output), redirect_stderr(error), self.assertRaises(SystemExit) as stopped:
                    server_setup.main()
                self.assertEqual(stopped.exception.code, 2)
                write.assert_not_called()
                self.assertIn('No password was saved', error.getvalue())


if __name__ == '__main__':
    unittest.main()
