"""Deployment upgrade validates before mutation and never replaces private state."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from deploy.server import upgrade as updater
from tools.package_release import package


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.release, self.installed = self.folder / 'release', self.folder / 'installed'
        for root, files in [(self.release, {'app/main.py': b'new', 'app/added.py': b'added', 'ui/dist/index.html': b'new UI', 'configs/account.json': b'defaults'}),
                            (self.installed, {'app/main.py': b'old', 'configs/account.json': b'custom', 'secrets/key': b'PRIVATE',
                                              'runs/orders.json': b'LEDGER', '.venv/pyvenv.cfg': b'ENV', 'app/still-needed.py': b'keep'})]:
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        self.archive = package(self.release, self.folder / 'out', 'server')

    def tearDown(self):
        self.temp.cleanup()

    def digest(self):
        return sha256(self.archive.read_bytes()).hexdigest()

    def test_dry_run_does_not_modify_files_and_apply_preserves_configuration_and_ledger(self):
        result = updater.upgrade(self.installed, self.archive, self.digest())
        self.assertEqual(result['status'], 'validated')
        self.assertEqual((self.installed / 'app/main.py').read_bytes(), b'old')
        self.assertFalse((self.installed / 'runs/upgrades').exists())
        result = updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        self.assertEqual(result['status'], 'installed')
        for name, content in {'app/main.py': b'new', 'app/added.py': b'added', 'configs/account.json': b'custom',
                              'secrets/key': b'PRIVATE', 'runs/orders.json': b'LEDGER', '.venv/pyvenv.cfg': b'ENV', 'app/still-needed.py': b'keep'}.items():
            self.assertEqual((self.installed / name).read_bytes(), content)
        backup = Path(result['backup'])
        self.assertEqual((backup / 'source/app/main.py').read_bytes(), b'old')
        self.assertEqual((backup / 'release-defaults/configs/account.json').read_bytes(), b'defaults')

    def test_wrong_archive_checksum_and_active_service_fail_without_changes(self):
        with self.assertRaises(ValueError):
            updater.upgrade(self.installed, self.archive, '0' * 64, apply=True)
        with self.assertRaisesRegex(ValueError, 'running'):
            updater.upgrade(self.installed, self.archive, self.digest(), apply=True,
                            stopped_check=lambda: (_ for _ in ()).throw(ValueError('running')))
        self.assertFalse((self.installed / 'runs/upgrades').exists())

    def test_new_config_stays_in_review_defaults(self):
        (self.installed / 'configs/account.json').unlink()
        updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        self.assertFalse((self.installed / 'configs/account.json').exists())

    def test_partial_write_failure_rolls_back_changed_and_created_files(self):
        real_write = updater.atomic_write
        calls = 0
        def fail_once(path, content, mode):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('disk full')
            return real_write(path, content, mode)
        with patch.object(updater, 'atomic_write', side_effect=fail_once), self.assertRaises(OSError):
            updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        self.assertEqual((self.installed / 'app/main.py').read_bytes(), b'old')
        self.assertFalse((self.installed / 'app/added.py').exists())
        journal = next((self.installed / 'runs/upgrades').glob('*/upgrade.json'))
        self.assertEqual(json.loads(journal.read_text())['status'], 'rolled_back')

    def test_unlisted_member_tamper_and_traversal_are_rejected_even_with_new_archive_hash(self):
        original = self.archive.read_bytes()
        for name, content in [('Trading-bot/app/unlisted.py', b'bad'), ('Trading-bot/../escape.py', b'bad'),
                              ('Trading-bot/secrets/private.json', b'bad'), ('Trading-bot/app/main.py', b'tampered')]:
            self.archive.write_bytes(original)
            with zipfile.ZipFile(self.archive, 'a') as bundle:
                bundle.writestr(name, content)
            with self.assertRaises(ValueError):
                updater.upgrade(self.installed, self.archive, self.digest(), apply=True)
            self.assertEqual((self.installed / 'app/main.py').read_bytes(), b'old')

    def test_links_in_archive_and_target_are_not_followed(self):
        with zipfile.ZipFile(self.archive, 'a') as bundle:
            link = zipfile.ZipInfo('Trading-bot/app/link.py')
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            bundle.writestr(link, '/etc/passwd')
        with self.assertRaises(ValueError):
            updater.validate_archive(self.archive, self.digest())
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda p: p.name == 'app'):
            with self.assertRaises(ValueError):
                updater.safe_target(self.installed.resolve(), 'app/main.py')

    def test_manifest_covers_start_here_and_archive_has_external_checksum(self):
        validated = updater.validate_archive(self.archive, self.digest())
        self.assertIn('START-HERE.txt', validated)
        self.assertEqual(self.archive.with_suffix('.zip.sha256').read_text().split()[0], self.digest())

    def test_manual_rollback_restores_public_source_only(self):
        result = updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        preview = updater.rollback(self.installed, result['backup'])
        self.assertEqual(preview['status'], 'rollback_validated')
        self.assertEqual((self.installed / 'app/main.py').read_bytes(), b'new')
        updater.rollback(self.installed, result['backup'], apply=True, stopped_check=lambda: None)
        self.assertEqual((self.installed / 'app/main.py').read_bytes(), b'old')
        self.assertFalse((self.installed / 'app/added.py').exists())
        self.assertEqual((self.installed / 'runs/orders.json').read_bytes(), b'LEDGER')

    def test_manual_rollback_refuses_overwriting_later_edits_and_tampered_backups(self):
        result = updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        (self.installed / 'app/main.py').write_bytes(b'newer user edits')
        with self.assertRaises(ValueError):
            updater.rollback(self.installed, result['backup'], apply=True, stopped_check=lambda: None)
        (self.installed / 'app/main.py').write_bytes(b'new')
        (Path(result['backup']) / 'source/app/main.py').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            updater.rollback(self.installed, result['backup'], apply=True, stopped_check=lambda: None)

    def test_stopped_check_rejects_unavailable_bus_and_lingering_workers(self):
        result = lambda code, text: SimpleNamespace(returncode=code, stdout=text)
        with patch.object(updater.os, 'name', 'posix'), patch.object(updater.subprocess, 'run') as run:
            run.return_value = result(1, '')
            with self.assertRaises(ValueError):
                updater.require_stopped()
            run.side_effect = [result(3, 'inactive\n'), result(0, '/user.slice/test\n')]
            with patch.object(updater, 'Path') as path:
                procs = path.return_value.__truediv__.return_value.__truediv__.return_value
                procs.exists.return_value = True
                procs.read_text.return_value = '1234\n'
                with self.assertRaisesRegex(ValueError, 'worker'):
                    updater.require_stopped()
            run.side_effect = [result(3, 'inactive\n'), result(0, '')]
            updater.require_stopped()

    def install_old_manifest(self):
        path = self.installed / 'deploy/desktop/obsolete.ps1'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'old launcher')
        (self.installed / 'release-manifest.json').write_text(json.dumps({
            'format_version': 2, 'sha256': {'deploy/desktop/obsolete.ps1': sha256(path.read_bytes()).hexdigest(),
                                           'configs/account.json': sha256(b'custom').hexdigest()}}))
        return path

    def test_managed_obsolete_files_are_removed_and_can_be_restored(self):
        obsolete = self.install_old_manifest()
        plan = updater.upgrade(self.installed, self.archive, self.digest())
        self.assertEqual(plan['removed_files'], ['deploy/desktop/obsolete.ps1'])
        self.assertTrue(obsolete.exists())
        result = updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        self.assertFalse(obsolete.exists())
        self.assertEqual((self.installed / 'app/still-needed.py').read_bytes(), b'keep')
        updater.rollback(self.installed, result['backup'], apply=True, stopped_check=lambda: None)
        self.assertEqual(obsolete.read_bytes(), b'old launcher')
        self.assertEqual((self.installed / 'configs/account.json').read_bytes(), b'custom')

    def test_obsolete_user_edits_or_recreated_file_are_not_overwritten(self):
        obsolete = self.install_old_manifest()
        obsolete.write_bytes(b'user edits')
        with self.assertRaisesRegex(ValueError, 'local edits'):
            updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        obsolete.write_bytes(b'old launcher')
        result = updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        obsolete.write_bytes(b'recreated after upgrade')
        with self.assertRaisesRegex(ValueError, 'changed since'):
            updater.rollback(self.installed, result['backup'], apply=True, stopped_check=lambda: None)

    def test_failed_rollback_restores_the_new_release_and_keeps_removed_files_absent(self):
        obsolete = self.install_old_manifest()
        result = updater.upgrade(self.installed, self.archive, self.digest(), apply=True, stopped_check=lambda: None)
        real_write = updater.atomic_write
        def fail_once(path, content, mode):
            if path.name == 'obsolete.ps1':
                raise OSError('disk error')
            return real_write(path, content, mode)
        with patch.object(updater, 'atomic_write', side_effect=fail_once), self.assertRaises(OSError):
            updater.rollback(self.installed, result['backup'], apply=True, stopped_check=lambda: None)
        self.assertFalse(obsolete.exists())
        self.assertEqual((self.installed / 'app/main.py').read_bytes(), b'new')


if __name__ == '__main__':
    unittest.main()
