"""Verification compares protected bytes without exposing credentials or hashes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.server import verify_deployment as verification


class DeploymentVerificationTests(unittest.TestCase):
    def test_protected_baseline_is_private_and_output_contains_only_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            (root / 'secrets').mkdir(parents=True)
            (root / 'configs').mkdir()
            secret = root / 'secrets/key.json'
            secret.write_text('secret-value-that-must-not-be-printed')
            (root / 'configs/choice.json').write_text('{"mode":"paper"}')
            baseline = Path(temporary) / 'before.json'
            with patch.object(verification, 'unit_state', return_value={}), patch.object(verification, 'process_state', return_value={}), patch.object(verification, 'health', return_value={}):
                result = verification.verify(root, baseline=baseline, capture=True)
                self.assertTrue(result['baseline_captured'])
                self.assertEqual(result['protected_file_count'], 2)
                compared = verification.verify(root, baseline=baseline)
                self.assertTrue(compared['protected_files_unchanged'])
                secret.write_text('replacement-secret')
                changed = verification.verify(root, baseline=baseline)
                self.assertFalse(changed['protected_files_unchanged'])
                self.assertEqual(changed['protected_changed_count'], 1)
                self.assertFalse(changed['automation']['enabled'])
                output = json.dumps([result, compared, changed])
                self.assertNotIn('secret-value-that-must-not-be-printed', output)
                self.assertNotIn('replacement-secret', output)
                self.assertNotIn('secrets/key.json', output)
                for value in json.loads(baseline.read_text())['files'].values():
                    self.assertNotIn(value, output)
                with self.assertRaises(FileExistsError):
                    verification.verify(root, baseline=baseline, capture=True)


if __name__ == '__main__':
    unittest.main()
