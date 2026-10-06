"""Verification compares protected bytes without exposing credentials or hashes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.server import verify_deployment as verification


class DeploymentVerificationTests(unittest.TestCase):
    def test_managed_worker_processes_are_counted_without_other_projects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, proc = Path(temporary) / 'project', Path(temporary) / 'proc'
            root.mkdir(); proc.mkdir()
            for pid, parent, module in ((10, 1, 'app.portal_server'), (20, 10, 'app.strategy_worker'),
                                        (21, 20, 'app.strategy_select'), (30, 10, 'unrelated'),
                                        (40, 1, 'app.strategy_worker')):
                folder = proc / str(pid); folder.mkdir()
                (folder / 'stat').write_text(f'{pid} (python) S {parent} 0 0')
                (folder / 'cmdline').write_bytes(b'python\0-m\0' + module.encode() + b'\0')
            result = verification.process_state(root, 10, proc=proc)
            self.assertTrue(result['controller_present'])
            self.assertEqual(result['child_process_count'], 3)
            self.assertEqual(result['active_operation_process_count'], 2)

    def test_instance_verification_only_reads_counts_and_does_not_recover_pending_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(verification.strategy_manager_state(root)['running_count'], 0)
            path = root / 'runs/strategy_manager/state.json'; path.parent.mkdir(parents=True)
            path.write_text(json.dumps(dict(schema=1, instances=[{'enabled':True,'name':'private-name'}],
                                            pending={'private-broker':'private-cycle-path'})))
            before = path.read_bytes()
            result = verification.strategy_manager_state(root)
            self.assertEqual(result['running_count'], 1)
            self.assertEqual(result['pending_account_count'], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn('private-', json.dumps(result))
            path.write_text('{broken')
            self.assertTrue(verification.strategy_manager_state(root)['corrupt'])
            self.assertEqual(path.read_text(), '{broken')

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
