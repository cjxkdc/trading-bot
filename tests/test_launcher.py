"""Launcher boundaries: no accidental live route, stale data or blind retries."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import launcher
from app import launcher_worker as worker
from framework.execution import ExecutionBlocked
from platforms.data.futu_technology import SCHEMA as PREVIEW_SCHEMA
from platforms.data.snapshot import demo_snapshot, parse_snapshot, SnapshotFile

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 1, 13, 35, tzinfo=timezone.utc)


class LauncherWorkerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='trading launcher ')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'configs').mkdir()
        (self.root / 'configs/data_sources.json').write_bytes((ROOT / 'configs/data_sources.json').read_bytes())
        self.cfg = json.loads((ROOT / 'configs/portfolio_config.json').read_text(encoding='utf-8'))
        self.cfg['broker'] = {'type': 'futu_paper', 'config_dir': 'secrets/futu', 'state_dir': 'runs/futu-paper'}
        self.write_config()
        self.raw = demo_snapshot()
        self.raw['synthetic'] = False
        for i, (stock, quote) in enumerate(zip(self.raw['stocks'], self.raw['quotes'])):
            stock['asset_id'] = stock['ticker'] = quote['asset_id'] = f'TEST{i}'
        self.path = self.root / 'snapshot with spaces.json'
        self.write_snapshot()
        self.inspect = patch.object(worker, 'inspect_broker', return_value={
            'status': 'connected', 'account_environment': 'SIMULATE', 'issues': []})
        self.reader = self.inspect.start()
        self.addCleanup(self.inspect.stop)
        self.session = patch.object(worker, 'open_broker_paper_session')
        self.open_session = self.session.start()
        self.addCleanup(self.session.stop)

    def write_config(self):
        (self.root / 'configs/futu_paper_config.json').write_text(json.dumps(self.cfg), encoding='utf-8')

    def write_snapshot(self):
        self.path.write_text(json.dumps(self.raw), encoding='utf-8')

    def request(self, action, **kwargs):
        return worker.dispatch(action, root=self.root, snapshot_path=self.path, now=NOW, **kwargs)

    def preview(self):
        result = self.request('preview')
        self.assertEqual(result['status'], 'previewed', result)
        return result

    def test_startup_is_read_only_offline_and_does_not_create_broker_state(self):
        self.request('offline')
        self.reader.assert_called_once_with('futu_paper', self.root / 'secrets/futu', connect=False)
        self.open_session.assert_not_called()
        self.assertFalse((self.root / 'runs/futu-paper').exists())

    def test_live_or_relocated_ledger_rejected_before_any_connection(self):
        for change in ({'type': 'futu_live'}, {'type': 'moomoo_paper'},
                       {'state_dir': 'runs/different'}, {'config_dir': 'secrets/futu_live'}):
            with self.subTest(change=change):
                cfg = deepcopy(self.cfg)
                cfg['broker'].update(change)
                (self.root / 'configs/futu_paper_config.json').write_text(json.dumps(cfg), encoding='utf-8')
                self.assertEqual(self.request('status')['status'], 'blocked')
        self.reader.assert_not_called()
        self.open_session.assert_not_called()

    def test_connection_requires_explicit_simulation_account_binding(self):
        result = self.request('status')
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('绑定', result['message'])
        self.reader.assert_not_called()
        with patch.object(worker, 'bound_account'):
            self.assertEqual(self.request('status')['status'], 'connected')
        self.reader.assert_called_once_with('futu_paper', self.root / 'secrets/futu', connect=True)
        self.open_session.assert_not_called()

    def test_preview_never_connects_and_cannot_execute_without_token(self):
        result = self.preview()
        self.assertTrue(result['execution_ready'])
        self.assertEqual(len(result['targets']), 5)
        self.reader.assert_not_called()
        self.assertEqual(self.request('execute')['status'], 'paused')
        self.open_session.assert_not_called()

    def test_historical_synthetic_missing_and_stale_quotes_are_preview_only(self):
        base = deepcopy(self.raw)
        variants = []
        for field, value in [('synthetic', True), ('execution_time', '2026-08-31T09:35:00-04:00'),
                             ('quotes', [])]:
            raw = deepcopy(base)
            raw[field] = value
            if field == 'execution_time':
                for stock in raw['stocks']:
                    stock['price_as_of'] = '2026-08-28'
            variants.append(raw)
        raw = deepcopy(base)
        raw['quotes'][0]['as_of'] = '2026-09-01T09:30:00-04:00'
        variants.append(raw)
        raw = deepcopy(base)
        raw['stocks'][0]['ticker'] = 'DEMO-00'
        variants.append(raw)
        for raw in variants:
            with self.subTest(raw=list(raw)):
                self.raw = raw
                self.write_snapshot()
                result = self.preview()
                self.assertFalse(result['execution_ready'])
                self.assertEqual(self.request('execute', preview_token=result['preview_token'])['status'], 'paused')
        self.reader.assert_not_called()
        self.open_session.assert_not_called()

    def test_snapshot_and_config_changes_invalidate_preview(self):
        token = self.preview()['preview_token']
        self.raw['stocks'][0]['operating_income_ttm'] += 1
        self.write_snapshot()
        self.assertEqual(self.request('execute', preview_token=token)['status'], 'paused')
        token = self.preview()['preview_token']
        self.cfg['strategy']['count'] = 4
        self.write_config()
        self.assertEqual(self.request('execute', preview_token=token)['status'], 'paused')
        self.open_session.assert_not_called()

    def test_quote_expiry_between_preview_and_execute_blocks_connection(self):
        result = self.preview()
        later = datetime(2026, 9, 1, 13, 40, tzinfo=timezone.utc)
        result = worker.dispatch('execute', root=self.root, snapshot_path=self.path,
                                 now=later, preview_token=result['preview_token'])
        self.assertEqual(result['status'], 'paused')
        self.open_session.assert_not_called()

    def test_execute_uses_original_simulation_ledger_and_never_retries_pending(self):
        token = self.preview()['preview_token']
        broker = Mock(mode='broker_paper')
        journal = Mock()
        @contextmanager
        def session(*args):
            yield broker, journal
        self.open_session.side_effect = session
        with patch.object(worker, 'bound_account'), patch('framework.runtime.PortfolioRunner.execute') as execute:
            execute.side_effect = ExecutionBlocked('Order accepted: reconcile before continuing; no resubmit')
            result = self.request('execute', preview_token=token)
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(result['automatic_retry'])
        self.assertEqual(execute.call_count, 1)
        self.open_session.assert_called_once()
        self.assertEqual(self.open_session.call_args.args[0]['broker']['type'], 'futu_paper')
        self.assertEqual(self.open_session.call_args.args[2], self.root / 'runs/futu-paper')

    def test_demo_runs_real_local_engine_without_broker_network_or_ledger(self):
        result = self.request('demo')
        self.assertEqual(result['status'], 'complete', result)
        self.assertTrue(result['details']['synthetic'])
        self.assertEqual(result['details']['orders'], 5)
        out = Path(result['details']['output_directory'])
        self.assertTrue((out / 'state/journal.sqlite').is_file())
        self.assertFalse((self.root / 'runs/futu-paper').exists())
        self.reader.assert_not_called()
        self.open_session.assert_not_called()

    def test_bad_file_and_unexpected_error_produce_report_without_raw_traceback(self):
        self.path.write_text('{', encoding='utf-8')
        self.assertEqual(self.request('preview')['status'], 'blocked')
        with patch.object(worker, 'perform', side_effect=RuntimeError('secret account identifier')):
            result = self.request('execute')
        self.assertEqual(result['status'], 'paused')
        self.assertNotIn('secret account', json.dumps(result))

    def test_captured_snapshot_parse_matches_existing_file_provider(self):
        self.assertEqual(parse_snapshot(self.raw), SnapshotFile(self.path).load())

    def current_data(self):
        raw = deepcopy(self.raw)
        raw.update(schema=PREVIEW_SCHEMA, execution_allowed=False, candidate_count=len(raw['stocks']),
                   limitations=['Current observation, not publication time'], universe_id='test-current',
                   excluded=[], issues=[], collection_complete=True, price_as_of='2026-08-31',
                   finished_at=NOW.isoformat())
        for stock in raw['stocks']:
            stock.update(observed_at=NOW.isoformat(), industry_name=stock['industry'])
        return raw

    def test_current_data_can_screen_without_backdating_but_never_execute(self):
        self.raw = self.current_data()
        self.write_snapshot()
        result = self.preview()
        self.assertEqual(len(result['targets']), 5)
        self.assertFalse(result['execution_ready'])
        self.assertNotIn('preview_token', result)
        self.assertEqual(self.request('execute', preview_token='invented')['status'], 'paused')
        # Relabeling the file does not erase the explicit research-only marker.
        self.raw['schema'] = 'us_technology_daily_v1'
        with self.assertRaisesRegex(ValueError, 'Research-only'):
            parse_snapshot(self.raw)
        self.reader.assert_not_called()
        self.open_session.assert_not_called()

    def test_refresh_dispatches_only_collector_and_persists_latest_preview(self):
        self.raw = self.current_data()
        self.write_snapshot()
        collector = Mock()
        collector.collect.return_value = self.path
        progress = Mock()
        with patch.object(worker, 'build_snapshot_collector', return_value=collector) as factory:
            result = self.request('refresh', progress=progress)
        self.assertEqual(result['status'], 'previewed', result)
        self.assertFalse(result['execution_ready'])
        self.assertEqual(factory.call_args.args[0]['type'], 'futu_technology')
        self.assertEqual(collector.collect.call_args.kwargs['price_bars'], 253)
        self.assertIs(collector.collect.call_args.kwargs['progress'], progress)
        latest = json.loads((self.root / 'runs/data/futu-technology/latest.json').read_text())
        self.assertEqual(latest['path'], str(self.path))
        self.reader.assert_not_called()
        self.open_session.assert_not_called()

    def test_failed_refresh_preserves_previous_data_pointer(self):
        path = self.root / 'runs/data/futu-technology/latest.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"path":"previous"}')
        with patch.object(worker, 'build_snapshot_collector') as factory:
            factory.return_value.collect.side_effect = ValueError('insufficient quota')
            result = self.request('refresh')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(json.loads(path.read_text())['path'], 'previous')

    def test_massive_refresh_changes_data_source_without_using_an_account(self):
        self.raw = self.current_data()
        self.raw.update(schema='current_equity_research_v1', provider='massive')
        self.write_snapshot()
        with patch.object(worker, 'build_snapshot_collector') as factory:
            factory.return_value.collect.return_value = self.path
            result = self.request('refresh', data_source='massive')
        self.assertEqual(result['status'], 'previewed', result)
        self.assertEqual(factory.call_args.args[0]['type'], 'massive')
        self.assertTrue(factory.return_value.for_request.called)
        self.assertEqual(result['details']['provider'], 'massive')
        self.assertFalse(result['execution_ready'])
        self.assertNotIn('preview_token', result)
        self.assertEqual(self.request('execute', preview_token='fake')['status'], 'paused')
        self.reader.assert_not_called()
        self.open_session.assert_not_called()

    def test_massive_key_is_saved_locally_and_absent_from_report(self):
        key = 'TEST_KEY_NEVER_USE_1234567890'
        result = self.request('configure-data', data_source='massive',
                              credentials={'api_key': key, 'requests_per_minute': 5})
        self.assertEqual(result['status'], 'ready', result)
        self.assertNotIn(key, json.dumps(result))
        saved = json.loads((self.root / 'secrets/massive/connection.json').read_text())
        self.assertEqual(saved['api_key'], key)
        self.reader.assert_not_called()
        self.open_session.assert_not_called()


class LauncherProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='launch space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python',
                     'app/launcher_worker.py', 'configs/futu_paper_config.json', 'data & not a command.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.snapshot = self.root / 'data & not a command.json'

    def test_command_is_argument_list_with_project_venv_and_no_live_or_shell_route(self):
        command = launcher.backend_command(self.root, 'execute', self.snapshot, 'checked-token')
        self.assertEqual(command[2:4], ['-m', 'app.launcher_worker'])
        self.assertEqual(command[4], 'execute')
        self.assertEqual(command[-4:], ['--snapshot', str(self.snapshot), '--preview-token', 'checked-token'])
        with self.assertRaises(ValueError):
            launcher.backend_command(self.root, 'broker-live', self.snapshot)
        with self.assertRaises(ValueError):
            launcher.backend_command(self.root, 'execute', self.snapshot)

    def test_frozen_root_uses_exe_location_not_temporary_bundle(self):
        with patch.object(launcher.sys, 'frozen', True, create=True), patch.object(launcher.sys, 'executable', str(self.root / 'Trading Bot.exe')):
            self.assertEqual(launcher.project_root(), self.root)

    def test_child_does_not_inherit_bundled_python_or_tk_library_paths(self):
        with patch.dict(os.environ, {'PYTHONHOME': 'wrong', 'PYTHONPATH': 'wrong', 'TCL_LIBRARY': 'wrong', 'TK_LIBRARY': 'wrong'}):
            env = launcher.external_environment()
        for name in ('PYTHONHOME', 'PYTHONPATH', 'TCL_LIBRARY', 'TK_LIBRARY'):
            self.assertNotIn(name, env)
        self.assertEqual(env['PYTHONIOENCODING'], 'utf-8')

    def test_worker_never_retries_or_kills_a_trading_process(self):
        report = {'launcher_protocol': 1, 'action': 'execute', 'status': 'paused', 'message': 'Order pending', 'automatic_retry': False}
        process = Mock(returncode=2)
        process.communicate.return_value = (json.dumps(report), 'discard unstructured stderr')
        with patch.object(launcher.subprocess, 'Popen', return_value=process) as popen:
            result = launcher.run_worker(self.root, 'execute', self.snapshot, 'token')
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(popen.call_count, 1)
        self.assertFalse(popen.call_args.kwargs['shell'])
        process.communicate.assert_called_once_with(timeout=None)
        process.kill.assert_not_called()

    def test_worker_crash_does_not_become_success_or_expose_stderr(self):
        process = Mock(returncode=1)
        process.communicate.return_value = ('not json', 'private identifier')
        with patch.object(launcher.subprocess, 'Popen', return_value=process):
            result = launcher.run_worker(self.root, 'execute', self.snapshot, 'token')
        self.assertEqual(result['status'], 'paused')
        self.assertNotIn('private identifier', str(result))
        process.kill.assert_not_called()

    def test_only_read_only_timeout_is_terminated(self):
        process = Mock(returncode=-1)
        process.communicate.side_effect = [subprocess.TimeoutExpired('worker', 60), ('', '')]
        with patch.object(launcher.subprocess, 'Popen', return_value=process):
            result = launcher.run_worker(self.root, 'status')
        self.assertEqual(result['status'], 'blocked')
        process.kill.assert_called_once()

    def test_refresh_streams_progress_and_accepts_only_final_protocol_result(self):
        report = {'launcher_protocol': 1, 'action': 'refresh', 'status': 'previewed',
                  'message': 'Data ready', 'execution_ready': False}
        process = Mock(returncode=0)
        process.stdout = iter(['nonprotocol SDK output\n',
            json.dumps({'launcher_progress': 1, 'message': '5/778'}) + '\n',
            json.dumps(report) + '\n', 'irrelevant trailing line\n'])
        progress = Mock()
        with patch.object(launcher.subprocess, 'Popen', return_value=process) as popen:
            result = launcher.run_worker(self.root, 'refresh', progress=progress)
        self.assertEqual(result, report)
        progress.assert_called_once_with('5/778')
        self.assertEqual(popen.call_args.kwargs['stderr'], subprocess.STDOUT)
        process.communicate.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_called_once()

    def test_refresh_crash_after_progress_does_not_report_success(self):
        process = Mock(returncode=1)
        process.stdout = iter([json.dumps({'launcher_progress': 1, 'message': '5/778'})])
        with patch.object(launcher.subprocess, 'Popen', return_value=process):
            result = launcher.run_worker(self.root, 'refresh')
        self.assertEqual(result['status'], 'blocked')

    def test_key_uses_stdin_never_command_line_or_logs(self):
        report = {'launcher_protocol': 1, 'action': 'configure-data', 'status': 'ready', 'message': 'Saved'}
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(report), '')
        secret = {'api_key': 'TEST_KEY_NEVER_USE_1234567890', 'requests_per_minute': 5}
        with patch.object(launcher.subprocess, 'Popen', return_value=process) as popen:
            result = launcher.run_worker(self.root, 'configure-data', data_source='massive', credentials=secret)
        self.assertNotIn(secret['api_key'], str(popen.call_args))
        self.assertEqual(json.loads(process.communicate.call_args.kwargs['input']), secret)
        self.assertNotIn(secret['api_key'], str(result))

    def test_second_window_is_blocked_until_first_releases_project_lock(self):
        with launcher.single_instance(self.root):
            with self.assertRaises(ValueError):
                with launcher.single_instance(self.root):
                    self.fail('A second GUI would duplicate user operations')
        with launcher.single_instance(self.root):
            pass


if __name__ == '__main__':
    unittest.main()
