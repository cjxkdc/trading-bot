"""Regression tests for the 2026-10 review fixes.

Covers: pre-submission failures that must not retire an intent, the explicit
"never transmitted" closure, replay-broker rejections, Massive row-level
validation, readiness for a no-orders preview, the strategy worker transport
guard and the automation binding check.
"""
from datetime import timedelta
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.automation import Automation, code_revision
from app.readiness import operation_states
from data.common import DataIssue, describe_issue
from data.massive_batch import grouped_rows
from framework.contracts import OrderState, SubmissionNotAttempted, SubmissionRejected
from framework.execution import Executor, ExecutionBlocked
from framework.journal import Journal
from framework.trading import ExecutionPolicy, Fees, Quote, plan_orders
from brokers.paper import PaperBroker, PaperSubmissionRejected
from test_execution import NOW, decision
from test_massive_batch import bar, grouped


class SubmissionNotAttemptedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.quotes = {s: Quote(s, D(99), D(100), NOW) for s in ('A', 'B')}
        self.policy, self.fees = ExecutionPolicy(max_gross=D('.9'), max_weight=D('.45')), Fees()
        PaperBroker.create(root / 'paper.sqlite', D(10000))
        self.broker = PaperBroker(root / 'paper.sqlite', self.quotes, NOW, self.fees)
        self.journal = Journal(root / 'journal.sqlite')
        self.addCleanup(self.broker.close); self.addCleanup(self.journal.close)
        self.baseline = self.broker.account()
        self.orders = plan_orders(decision(), self.baseline, self.quotes, NOW, self.policy, self.fees)
        self.proxy = Mock(wraps=self.broker)
        self.proxy.mode = 'broker_paper'
        self.proxy.execution_time = Mock(return_value=NOW)

    def run_batch(self):
        return Executor(self.proxy, self.journal, self.policy, self.fees).run(
            decision(), self.quotes, NOW, approved_orders=self.orders, expected_account=self.baseline)

    def test_not_attempted_keeps_the_intent_planned_and_batch_continues_later(self):
        self.proxy.submit.side_effect = SubmissionNotAttempted('account query failed')
        with self.assertRaisesRegex(ExecutionBlocked, 'not attempted'):
            self.run_batch()
        status, state = self.journal.status(self.orders[0])
        self.assertEqual((status, state), ('planned', None))
        audit = self.journal.db.execute("SELECT action FROM execution_audit").fetchall()
        self.assertEqual(audit, [('release_unattempted_claim',)])
        report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
        self.assertEqual(report['recovery_state'], 'ready')
        # Once the transient condition clears, the same frozen batch continues.
        self.proxy.submit.side_effect = None
        self.run_batch()
        self.assertEqual(self.journal.status(self.orders[0])[0], 'filled')
        self.assertEqual(self.proxy.submit.call_count, 3)

    def test_rejected_still_retires_the_intent(self):
        self.proxy.submit.side_effect = SubmissionRejected('bad symbol')
        with self.assertRaises(SubmissionRejected):
            self.run_batch()
        self.assertEqual(self.journal.status(self.orders[0])[0], 'rejected')
        self.assertTrue(self.journal.rejected_before_transmission(self.orders[0]))

    def stranded_unknown(self):
        """An intent whose send produced no acknowledgement and no broker record."""
        self.proxy.submit.side_effect = lambda o: OrderState(o.client_id, None, 'unknown', D(0))
        with self.assertRaises(ExecutionBlocked):
            self.run_batch()
        client_id = self.orders[0].client_id
        self.assertEqual(self.journal.status(self.orders[0])[0], 'unknown')
        self.proxy.query.side_effect = lambda ident: OrderState(ident, None, 'unknown', D(0))
        self.proxy.open_orders.side_effect = lambda: ('unknown:' + client_id,)
        self.proxy.read_only = True
        return client_id

    def test_reconcile_reports_untransmitted_unknowns(self):
        client_id = self.stranded_unknown()
        report = Executor(self.proxy, self.journal, self.policy, self.fees).reconcile()
        self.assertEqual(report['recovery_state'], 'blocked')
        self.assertEqual(report['untransmitted'], 1)
        item = next(i for i in report['orders'] if i['client_id'] == client_id)
        self.assertIsNone(item['broker_id'])
        self.assertIsNone(item['transmission_started'])

    def test_close_untransmitted_needs_broker_proof_then_unblocks_the_account(self):
        client_id = self.stranded_unknown()
        executor = Executor(self.proxy, self.journal, self.policy, self.fees)
        self.proxy.transmission_evidence = Mock(return_value={'transmitted': True, 'checked_days': []})
        self.proxy.retire_untransmitted = Mock()
        with self.assertRaisesRegex(ExecutionBlocked, 'does not prove'):
            executor.close_untransmitted()
        self.assertEqual(self.journal.status(self.orders[0])[0], 'unknown')
        self.proxy.transmission_evidence = Mock(return_value={'transmitted': False, 'checked_days': ['2026-09-01', '2026-09-02']})
        report = executor.close_untransmitted()
        self.assertEqual(report['newly_closed'], 1)
        self.assertEqual(self.journal.status(self.orders[0])[0], 'closed')
        self.proxy.retire_untransmitted.assert_called_once_with(client_id)
        audit = json.loads(self.journal.db.execute(
            "SELECT body FROM execution_audit WHERE action='close_untransmitted'").fetchone()[0])
        self.assertEqual(audit['intents'][0]['evidence']['checked_days'], ['2026-09-01', '2026-09-02'])
        self.proxy.open_orders.side_effect = lambda: ()
        # The remaining planned intent can now be continued; nothing is blocked.
        self.assertEqual(executor.reconcile()['recovery_state'], 'ready')

    def test_close_untransmitted_refuses_other_unresolved_broker_orders(self):
        client_id = self.stranded_unknown()
        self.proxy.open_orders.side_effect = lambda: ('unknown:' + client_id, '12345')
        self.proxy.transmission_evidence = Mock(return_value={'transmitted': False})
        self.proxy.retire_untransmitted = Mock()
        with self.assertRaisesRegex(ExecutionBlocked, 'other unresolved'):
            Executor(self.proxy, self.journal, self.policy, self.fees).close_untransmitted()
        self.assertEqual(self.journal.status(self.orders[0])[0], 'unknown')


class ReplayBrokerTests(unittest.TestCase):
    def test_stale_quote_is_a_pre_submission_rejection_and_the_limit_is_configurable(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'paper.sqlite'
        PaperBroker.create(path, D(10000))
        stale = {'A': Quote('A', D(99), D(100), NOW - timedelta(seconds=300))}
        probe = PaperBroker(path, stale, NOW, Fees(), 1000)
        account = probe.account()
        probe.close()
        order = plan_orders(decision((('A', .45),)), account, stale, NOW,
                            ExecutionPolicy(max_gross=D('.9'), max_weight=D('.45'), max_quote_age_seconds=1000), Fees())[0]
        broker = PaperBroker(path, stale, NOW, Fees())
        self.addCleanup(broker.close)
        with self.assertRaises(PaperSubmissionRejected) as caught:
            broker.submit(order)
        self.assertIsInstance(caught.exception, SubmissionRejected)
        self.assertEqual(broker.db.execute('SELECT count(*) FROM orders').fetchone()[0], 0)
        broker.close()
        relaxed = PaperBroker(path, stale, NOW, Fees(), max_quote_age_seconds=1000)
        self.addCleanup(relaxed.close)
        self.assertEqual(relaxed.submit(order).status, 'filled')


class MassiveRowValidationTests(unittest.TestCase):
    def test_one_bad_stock_row_is_recorded_without_failing_the_day(self):
        from datetime import date
        day = date(2026, 10, 1)
        bad = dict(bar('AAPL', day, 50), h=10)  # high below close
        saved = grouped([bar('SPY', day, 100), bad, bar('MSFT', day, 70)])
        invalid = {}
        rows = grouped_rows(saved, day, {'AAPL', 'MSFT'}, invalid)
        self.assertEqual(rows['MSFT'], (70, 2e6))
        self.assertNotIn('AAPL', rows)
        self.assertEqual(invalid, {'AAPL': 'invalid_grouped_date_price_or_volume'})
        duplicated = grouped([bar('SPY', day, 100), bar('MSFT', day, 70), bar('MSFT', day, 71)])
        invalid = {}
        self.assertNotIn('MSFT', grouped_rows(duplicated, day, {'MSFT'}, invalid))
        self.assertEqual(invalid, {'MSFT': 'duplicate_or_invalid_grouped_ticker'})
        with self.assertRaises(DataIssue):  # The SPY reference row still has to be valid.
            grouped_rows(grouped([dict(bar('SPY', day, 100), h=10)]), day, {'MSFT'})

    def test_reason_codes_get_user_facing_text(self):
        self.assertIn('开高低收', describe_issue('invalid_grouped_date_price_or_volume'))
        self.assertIn('invalid_grouped_date_price_or_volume', describe_issue('invalid_grouped_date_price_or_volume'))
        self.assertEqual(describe_issue('已有数据采集任务正在运行'), '已有数据采集任务正在运行')
        self.assertIn('数据校验未通过', describe_issue('brand_new_code'))


class ReadinessTests(unittest.TestCase):
    def test_no_orders_preview_explains_why_execute_is_disabled(self):
        broker = {'enabled': True, 'capabilities': ['inspect', 'prepare', 'execute'], 'mode': 'broker_paper', 'execution_enabled': True}
        source = {'enabled': True, 'capabilities': ['collect', 'preview', 'paper_signal'], 'configured': True}
        preview = {'status': 'complete', 'no_orders': True, 'execution_ready': False}
        states = operation_states(broker=broker, source=source, snapshot={'id': 's'}, preview=preview, verified=True,
                                  account={'status': 'connected'}, preview_authorized=False)
        self.assertFalse(states['execute']['enabled'])
        self.assertEqual(states['execute']['blockers'][0]['code'], 'no_orders')
        self.assertTrue(states['close-untransmitted']['enabled'])


class StrategyWorkerTransportTests(unittest.TestCase):
    def test_non_object_json_lines_are_ignored_and_the_child_is_awaited(self):
        from app import task_client
        lines = ['{"portal_progress": 1, "message": "checking"}\n', '0\n', '[]\n', 'null\n',
                 '{"portal_protocol": 1, "action": "strategy-check", "status": "complete", "message": "ok"}\n']
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = iter(lines)
        process.returncode = 0
        progress = Mock()
        with patch.object(task_client.subprocess, 'Popen', return_value=process):
            result = task_client.run_strategy_worker(Path(tempfile.gettempdir()), 'strategy-check',
                                                     instance={'id': 'x'}, progress=progress)
        self.assertEqual(result['status'], 'complete')
        process.wait.assert_called_once()
        progress.assert_called_once_with('checking')


class AutomationBindingTests(unittest.TestCase):
    def test_viewing_another_account_does_not_pause_the_plan(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        registry = Mock()
        registry.context.return_value = 'ctx-1'
        plan = Automation(root, registry)
        plan.data.update(enabled=True, status='waiting', binding={
            'broker_id': 'futu_paper', 'source_id': 'massive', 'context': 'ctx-1',
            'code_revision': code_revision(root), 'broker_name': 'Futu', 'source_name': 'Massive', 'strategy_name': 's'})
        plan.validate_binding('moomoo_paper', 'futu')  # the user looks at another account
        self.assertTrue(plan.data['enabled'])
        registry.context.assert_called_with('futu_paper', 'massive')
        registry.context.return_value = 'ctx-2'  # the plan's own modules changed
        plan.validate_binding('futu_paper', 'massive')
        self.assertFalse(plan.data['enabled'])
        self.assertEqual(plan.data['status'], 'paused')


class AtomicReplaceTests(unittest.TestCase):
    def test_transient_lock_is_retried_and_the_write_still_lands(self):
        from app import atomic
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        target = Path(temp.name) / 'state.json'
        target.write_text('old', encoding='utf-8')
        temporary = target.with_name('.new.tmp')
        temporary.write_text('new', encoding='utf-8')
        original, attempts = Path.replace, []
        def flaky(self, other):
            attempts.append(other)
            if len(attempts) < 3:
                raise PermissionError(13, 'locked by a scanner')
            return original(self, other)
        with patch.object(Path, 'replace', flaky), patch.object(atomic.time, 'sleep') as sleep:
            atomic.replace_file(temporary, target)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(target.read_text(encoding='utf-8'), 'new')
        self.assertFalse(temporary.exists())

    def test_a_lock_that_never_clears_still_raises(self):
        from app import atomic
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        target = Path(temp.name) / 'state.json'
        temporary = target.with_name('.new.tmp')
        temporary.write_text('new', encoding='utf-8')
        with patch.object(Path, 'replace', side_effect=PermissionError(13, 'locked')), \
             patch.object(atomic.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError):
                atomic.replace_file(temporary, target)
        self.assertEqual(sleep.call_count, len(atomic.RETRY_DELAYS))
        self.assertFalse(target.exists())


class SecretFieldTests(unittest.TestCase):
    def test_redaction_names_come_from_manifests_without_building_descriptors(self):
        from app.module_registry import ModuleRegistry
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for folder, kind, name in (('data', 'snapshot_data', 'feed'), ('brokers', 'broker', 'desk')):
            (root / folder / 'plugins').mkdir(parents=True)
            entrypoint = f'{folder}.{name}_plugin:Control'
            (root / folder / 'plugins' / f'{name}.json').write_text(json.dumps({
                'type': name, 'kind': kind, 'version': '1', 'entrypoint': entrypoint,
                'control': {'entrypoint': entrypoint, 'settings_fields': [
                    {'name': f'{name}_token', 'type': 'string', 'secret': True},
                    {'name': f'{name}_host', 'type': 'string'}]}}), encoding='utf-8')
        registry = ModuleRegistry(root)
        with patch.object(ModuleRegistry, 'descriptor', side_effect=AssertionError('must not build descriptors')):
            self.assertEqual(registry.secret_fields(), {'feed_token', 'desk_token'})


class LogReaderTests(unittest.TestCase):
    SAMPLE = ('2026-10-10 09:00:01,100 ERROR trading_bot: 500 POST /api/compare incident=aaaa111111\n'
              'builtins.ValueError\n'
              '  File "app/workbench.py", line 10, in compare\n'
              '2026-10-10 09:05:02,200 WARNING uvicorn.error: shutting down\n'
              '2026-10-10 09:07:03,300 ERROR trading_bot: 422 POST /api/jobs incident=bbbb222222\n'
              'builtins.KeyError\n')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        folder = self.root / 'runs/portal/logs'
        folder.mkdir(parents=True)
        (folder / 'backend.log').write_text(self.SAMPLE, encoding='utf-8')

    def test_records_group_traceback_lines_and_come_back_newest_first(self):
        from app.logs import read_log
        result = read_log(self.root, 'backend')
        self.assertEqual([r['header'][:19] for r in result['records']],
                         ['2026-10-10 09:07:03', '2026-10-10 09:05:02', '2026-10-10 09:00:01'])
        self.assertEqual(result['records'][2]['lines'], ['builtins.ValueError', '  File "app/workbench.py", line 10, in compare'])
        self.assertEqual(result['records'][1]['lines'], [])
        self.assertFalse(result['truncated'])
        self.assertEqual([f['name'] for f in result['files']], ['portal', 'backend'])
        self.assertEqual(result['files'][0]['size'], 0)

    def test_an_incident_id_filters_to_its_record_and_limits_apply(self):
        from app.logs import read_log
        found = read_log(self.root, 'backend', query='AAAA111111')
        self.assertEqual(len(found['records']), 1)
        self.assertIn('incident=aaaa111111', found['records'][0]['header'])
        self.assertEqual(len(read_log(self.root, 'backend', limit=2)['records']), 2)
        self.assertEqual(read_log(self.root, 'portal')['records'], [])
        with self.assertRaises(ValueError):
            read_log(self.root, '../secrets')

    def test_control_plane_serves_logs_without_the_backend(self):
        from fastapi.testclient import TestClient
        from portal.controller import create_controller
        from portal.runtime import BackendRuntime
        client = TestClient(create_controller(self.root, port=8765, instance='test', runtime=BackendRuntime(self.root)),
                            base_url='http://127.0.0.1:8765')
        self.addCleanup(client.close)
        token = client.get('/api/session').json()['token']
        headers = {'X-Portal-Token': token}
        response = client.get('/api/runtime/logs?name=backend&query=bbbb222222', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['records']), 1)
        self.assertEqual(client.get('/api/runtime/logs?name=other', headers=headers).status_code, 409)
        self.assertEqual(client.get('/api/runtime/logs', headers={}).status_code, 403)


class _TopCloseStrategy:
    """Holds half the book in whichever stock closed highest; enough to check the ledger arithmetic."""
    from framework.portfolio import HistoryRequirements as _H
    history_requirements = _H(2, 1)

    def select(self, stocks):
        from framework.portfolio import StockTarget
        ranked = sorted(stocks, key=lambda s: -s.adjusted_closes[-1])
        return (StockTarget(ranked[0].asset_id, 0.5, 1.0),) if ranked else ()


def _stock(asset_id, day, closes):
    from framework.portfolio import StockSnapshot
    return StockSnapshot(asset_id, asset_id, day, day, 'tech', 1.0, tuple(float(c) for c in closes), (1e7,))


class ReplayEngineTests(unittest.TestCase):
    def test_whole_share_ledger_values_positions_at_each_snapshot(self):
        from datetime import date
        from backtests.replay import replay
        days = [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)]
        series = [(days[0], [_stock('A', days[0], [9, 10]), _stock('B', days[0], [10, 10])]),
                  (days[1], [_stock('A', days[1], [10, 12]), _stock('B', days[1], [10, 9])]),
                  (days[2], [_stock('A', days[2], [12, 15]), _stock('B', days[2], [9, 8])])]
        result = replay(series, _TopCloseStrategy(), max_gross=0.95, max_weight=0.5, initial_cash=1000,
                        fee_per_order=0, fee_bps=0, slippage_bps=0)
        self.assertEqual(result['provider'], 'local_replay')
        self.assertEqual([value for _, value in result['nav']], [1000.0, 1100.0, 1235.0])
        self.assertEqual(result['metrics']['total_orders'], 3.0)
        self.assertAlmostEqual(result['metrics']['total_return'], 0.235)
        self.assertEqual(result['metrics']['max_drawdown'], 0.0)
        self.assertEqual(result['final_positions'], {'A': 41})
        self.assertEqual([event['orders'] for event in result['rebalances']], [1, 1, 1])
        self.assertEqual(result['period'], {'start': '2026-06-30', 'end': '2026-08-31'})

    def test_costs_slippage_and_a_vanishing_stock_are_applied_and_flagged(self):
        from datetime import date
        from backtests.replay import replay
        days = [date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)]
        series = [(days[0], [_stock('A', days[0], [9, 10])]),
                  (days[1], [_stock('A', days[1], [10, 12])]),
                  (days[2], [_stock('B', days[2], [10, 10])])]
        result = replay(series, _TopCloseStrategy(), max_gross=0.95, max_weight=0.5, initial_cash=1000,
                        fee_per_order=1, fee_bps=10, slippage_bps=100, cadence='monthly')
        # Day 1: 50 shares at 10.10 (1% slippage) cost 505 + 1 + 0.505 fees.
        self.assertAlmostEqual(result['rebalances'][0]['cash_after'], round(1000 - 505 - 1 - 0.505, 2), places=2)
        self.assertTrue(any('A left the universe on 2026-08-31' in warning for warning in result['warnings']))
        # A is valued at its last known close (12) once it is gone from the snapshot.
        self.assertEqual(result['nav'][-1][0], '2026-08-31')
        with self.assertRaises(ValueError):
            replay(series[:1], _TopCloseStrategy(), max_gross=0.95, max_weight=0.5, initial_cash=1000,
                   fee_per_order=0, fee_bps=0, slippage_bps=0)

    def test_parsers_share_one_archive_entry_point(self):
        from backtests.replay import parse_any, parse_replay, rebalance_days
        from datetime import date
        payload = {'provider': 'local_replay', 'schema_version': 1, 'completion': 'reported_complete',
                   'period': {'start': '2026-06-30', 'end': '2026-08-31'},
                   'metrics': {'total_return': 0.1, 'annual_return': None, 'max_drawdown': 0.05, 'sharpe': 1.2, 'total_orders': 3.0}}
        normalized = parse_any(payload)
        self.assertEqual(normalized['provider'], 'local_replay')
        self.assertEqual(normalized['completion'], 'reported_complete')
        self.assertEqual(normalized['metrics']['total_orders'], 3.0)
        self.assertEqual(parse_replay(payload), normalized)
        with self.assertRaises(ValueError):
            parse_any({})
        with self.assertRaises(ValueError):
            parse_replay(dict(payload, metrics=dict(payload['metrics'], max_drawdown=1.5)))
        self.assertEqual(rebalance_days([date(2026, 6, 30), date(2026, 7, 1), date(2026, 7, 15), date(2026, 8, 3)], 'monthly'),
                         [date(2026, 6, 30), date(2026, 7, 1), date(2026, 8, 3)])


class ReplayWorkbenchTests(unittest.TestCase):
    def setUp(self):
        import shutil
        from app import assembly
        from app.workbench import PortalService, atomic_json
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source_root = Path(__file__).resolve().parents[1]
        for folder in ('configs', 'strategies', 'framework'):
            shutil.copytree(source_root / folder, self.root / folder, ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('data', 'brokers'):
            shutil.copytree(source_root / folder / 'plugins', self.root / folder / 'plugins')
        config_path = self.root / 'configs/portfolio_config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['strategy'] = {'type': 'tech_profit_momentum', 'count': 5, 'weight': .19}
        atomic_json(config_path, config)
        patcher = patch('app.assembly.ROOT', self.root); patcher.start(); self.addCleanup(patcher.stop)
        self.service = PortalService(self.root, worker=lambda *a, **kw: self.fail('replay must not call a worker'))
        self.addCleanup(self.service.close) if hasattr(self.service, 'close') else None
        self.service.select_source('snapshot_file')

    def snapshot(self, day, bump):
        from data.snapshot import demo_snapshot
        value = demo_snapshot()
        value['execution_time'] = f'{day}T09:35:00-04:00'
        for index, stock in enumerate(value['stocks']):
            stock['price_as_of'] = day
            stock['known_on'] = day
            stock['adjusted_closes'] = [*stock['adjusted_closes'][:-1], 100.0 + bump * (index + 1)]
        return json.dumps(value).encode()

    def test_replay_runs_on_saved_snapshots_and_lands_in_the_experiment_archive(self):
        self.assertEqual(self.service.replay_series()[1], [])
        for day, bump in (('2026-06-30', 0), ('2026-07-31', 2), ('2026-08-31', 4)):
            self.service.upload_snapshot(self.snapshot(day, bump))
        source, series = self.service.replay_series()
        self.assertEqual(source, 'snapshot_file')
        self.assertEqual([item['date'] for item in series], ['2026-06-30', '2026-07-31', '2026-08-31'])
        with self.assertRaises(Exception):
            self.service.replay_backtest({'start': '2026-08-01', 'end': '2026-08-31', 'slippage_bps': 10})
        result = self.service.replay_backtest({'start': '2026-06-01', 'end': '2026-09-30', 'slippage_bps': 10,
                                               'expected_strategy_type': 'tech_profit_momentum'})
        self.assertEqual(result['snapshot_count'], 3)
        self.assertEqual(result['rebalance_count'], 3)
        self.assertGreater(result['metrics']['total_orders'], 0)
        listed = self.service.experiments()
        self.assertEqual(listed[0]['id'], result['experiment'])
        self.assertEqual(listed[0]['results'][0]['provider'], 'local_replay')
        self.assertEqual(listed[0]['results'][0]['id'], result['id'])
        self.assertNotIn('invalid', listed[0]['results'][0])
        second = self.service.replay_backtest({'start': '2026-06-01', 'end': '2026-09-30', 'slippage_bps': 50})
        report = self.service.compare([result['id'], second['id']])
        self.assertEqual(len(report['rows']), 2)
        self.assertIn('slippage_bps', report['comparison_differences'])


class PlanMigrationTests(unittest.TestCase):
    def setUp(self):
        import shutil
        from app.workbench import PortalService
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source_root = Path(__file__).resolve().parents[1]
        for folder in ('configs', 'strategies', 'framework'):
            shutil.copytree(source_root / folder, self.root / folder, ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('data', 'brokers'):
            shutil.copytree(source_root / folder / 'plugins', self.root / folder / 'plugins')
        patcher = patch('app.assembly.ROOT', self.root); patcher.start(); self.addCleanup(patcher.stop)
        self.service = PortalService(self.root, worker=lambda *a, **kw: self.fail('migration must not call a worker'))
        self.broker = self.service.registry.default('broker')
        self.plan = self.service.automation
        self.plan.data.update(enabled=True, status='waiting', binding={
            'broker_id': self.broker, 'source_id': 'massive', 'broker_name': 'Fixture broker', 'source_name': 'Massive',
            'strategy_name': '科技股盈利动量', 'context': self.service.registry.context(self.broker, 'massive'),
            'code_revision': code_revision(self.root)})
        self.plan.persist()

    def test_plan_becomes_a_stopped_instance_with_full_budget_and_the_plan_closes(self):
        from app.workbench import PortalError
        with self.assertRaises(PortalError):
            self.service.automation_migrate({'budget': '0.5'})
        result = self.service.automation_migrate({})
        item = self.service.strategy_instances.get(result['id'])
        self.assertEqual((item['broker_id'], item['source_id'], item['budget'], item['enabled'], item['status']),
                         (self.broker, 'massive', '1', False, 'stopped'))
        self.assertEqual(item['strategy']['type'], 'tech_profit_momentum')
        self.assertEqual(item['schedule']['cadence'], self.plan.data['config']['cadence'])
        self.assertIn('迁移自单策略计划', item['name'])
        self.assertFalse(self.plan.data['enabled'])
        self.assertEqual(self.plan.data['status'], 'disabled')
        self.assertEqual(self.plan.data['migrated_to'], result['id'])
        self.assertTrue(any('已迁移' in event['message'] for event in self.plan.data['events']))

    def test_migration_refuses_unfinished_batches_and_changed_bindings(self):
        from app.workbench import PortalError
        self.plan.data['cycles']['x'] = {'status': 'pending', 'broker_id': self.broker, 'day': '2026-10-01', 'context': 'c'}
        with self.assertRaisesRegex(PortalError, '未核对'):
            self.service.automation_migrate({})
        self.plan.data['cycles'].clear()
        self.plan.data['binding']['context'] = 'something-else'
        with self.assertRaisesRegex(PortalError, '绑定时不同'):
            self.service.automation_migrate({})
        self.assertEqual(self.service.strategy_instances.data['instances'], [])
        self.assertTrue(self.plan.data['enabled'])


class BackfillCollectorTests(unittest.TestCase):
    """A dated collection asks the vendor for the universe, details and bars as of that date."""
    def setUp(self):
        from datetime import date
        from app.assembly import strategy_data_requirements
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = strategy_data_requirements({'type': 'mom8', 'lookback': 20, 'skip': 2, 'trend_window': 20})
        self.as_of = date(2026, 9, 15)
        days = [date(2026, 8, 1) + timedelta(days=i) for i in range(45)]
        self.days = [d for d in days if d.weekday() < 5 and d < self.as_of][-21:]
        self.symbols = ['S' + str(i).zfill(2) for i in range(12)]
        self.queries = []

    def detail(self, symbol):
        index = int(symbol[1:]) if symbol.startswith('S') else 99
        return {'ticker': symbol, 'type': 'CS', 'locale': 'us', 'market': 'stocks', 'active': True,
                'primary_exchange': 'XNAS', 'currency_name': 'usd', 'list_date': '2000-01-01',
                'share_class_figi': 'ID' + symbol, 'market_cap': 10e9, 'sic_code': str(4000 + index // 4), 'cik': '123'}

    def close(self, symbol, index):
        if symbol == 'SPY': return 100 + index / 2
        if symbol == 'AAPL': return 1
        rank = int(symbol[1:])
        if index == len(self.days) - 3: return 200 - rank * 2
        return 200 if index == len(self.days) - 1 else 100

    def respond(self, req, timeout):
        from datetime import date
        from urllib.parse import parse_qs, urlsplit
        from data.massive import TICKERS, SPLITS
        from test_massive import Reply
        part = urlsplit(req.full_url)
        self.queries.append((part.path, parse_qs(part.query)))
        if part.path == TICKERS:
            raw = {'results': [self.detail(s) for s in self.symbols], 'status': 'OK'}
        elif part.path.startswith(TICKERS + '/'):
            raw = {'results': self.detail(part.path.rsplit('/', 1)[-1]), 'status': 'OK'}
        elif part.path == SPLITS:
            raw = {'results': [], 'status': 'OK'}
        elif part.path.startswith('/v2/aggs/grouped/'):
            day = date.fromisoformat(part.path.rsplit('/', 1)[-1]); index = self.days.index(day)
            raw = grouped([bar(s, day, self.close(s, index)) for s in ('SPY', *self.symbols)])['data']
        elif part.path.startswith('/v2/aggs/ticker/SPY/'):
            raw = {'ticker': 'SPY', 'adjusted': True, 'results': [
                {k: v for k, v in bar('SPY', d, self.close('SPY', i)).items() if k != 'T'} for i, d in enumerate(self.days)]}
        else:
            raise AssertionError('Unexpected endpoint: ' + part.path)
        return Reply(raw, req.full_url)

    def collect(self, as_of):
        from data.massive import MassiveClient, Settings
        from data.massive_provider import MassiveData
        from test_massive import Clock, KEY
        clock = Clock()
        opener = Mock(); opener.open.side_effect = self.respond
        def client(cfg, path, **kwargs):
            return MassiveClient(cfg, path, opener=opener, clock=clock.now, monotonic=clock.monotonic, sleep=clock.sleep, **kwargs)
        collector = MassiveData(self.root / 'secrets', Path(__file__).resolve().parents[1] / 'configs/massive.json',
                                self.root / 'cache').for_request(self.request)
        with patch('data.massive_provider.MassiveClient', side_effect=client), \
             patch('data.massive_provider.settings', return_value=Settings(KEY, 5)), \
             patch('data.massive_provider.utcnow', side_effect=clock.now):
            path = collector.collect(self.root / ('out-' + str(as_of)), as_of=as_of)
        return json.loads(path.read_text(encoding='utf-8'))

    def test_dated_collection_uses_point_in_time_listing_and_details_and_is_labelled(self):
        from data.massive import TICKERS
        raw = self.collect(self.as_of)
        end = str(self.days[-1])
        self.assertEqual((raw['price_as_of'], raw['as_of_requested']), (end, '2026-09-15'))
        self.assertFalse(raw['historical_point_in_time'])
        self.assertFalse(raw['execution_allowed'])
        self.assertTrue(any('历史回填' in text for text in raw['limitations']))
        self.assertTrue(raw['stocks'])
        self.assertTrue({s['ticker'] for s in raw['stocks']} <= set(self.symbols))
        self.assertTrue(all(day < '2026-09-15' for day in raw['sessions']))
        listing = [query for path, query in self.queries if path == TICKERS]
        self.assertEqual(listing[0]['date'], [end])
        details = [query for path, query in self.queries if path.startswith(TICKERS + '/') and not path.endswith('/AAPL')]
        self.assertTrue(details)
        self.assertTrue(all(query.get('date') == [end] for query in details))

    def test_backfill_date_must_be_in_the_past(self):
        from datetime import date
        with self.assertRaisesRegex(DataIssue, '早于今天'):
            self.collect(date(2026, 10, 2))
        self.assertEqual(self.queries, [])


class BackfillQueueTests(unittest.TestCase):
    def test_anchor_dates_coverage_and_estimate(self):
        from datetime import date
        from app.backfill import covered, estimate_requests, schedule_dates
        today = date(2026, 4, 1)
        self.assertEqual(schedule_dates(date(2026, 1, 15), date(2026, 4, 10), 'monthly', today), [date(2026, 2, 1), date(2026, 3, 1)])
        self.assertEqual(schedule_dates(date(2026, 3, 1), date(2026, 3, 1), 'monthly', today), [date(2026, 3, 1)])
        self.assertEqual(schedule_dates(date(2026, 3, 3), date(2026, 3, 20), 'weekly', date(2026, 3, 18)), [date(2026, 3, 9), date(2026, 3, 16)])
        self.assertEqual(covered(date(2026, 2, 1), [date(2026, 1, 10), date(2026, 1, 30)]), date(2026, 1, 30))
        self.assertIsNone(covered(date(2026, 3, 1), [date(2026, 1, 30), date(2026, 3, 1)]))
        one = estimate_requests(1, history_bars=253, details=100, income_required=True, listing_pages=6, cadence='monthly')
        two = estimate_requests(2, history_bars=253, details=100, income_required=True, listing_pages=6, cadence='monthly')
        self.assertEqual(one, 5 + 6 + 253 + 200)
        self.assertEqual(two - one, 5 + 6 + 23 + 200)

    def test_queue_persists_and_a_restart_pauses_an_unfinished_run(self):
        from app.backfill import Backfill
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        queue = Backfill(temp.name)
        self.assertIsNone(queue.public())
        dates = [{'as_of': '2026-02-01', 'status': 'pending', 'job_id': None, 'message': ''},
                 {'as_of': '2026-03-01', 'status': 'skipped', 'job_id': None, 'message': '已有快照'}]
        queue.create(source='massive', strategy_type='mom8', request_id='fp', cadence='monthly', start='2026-01-15',
                     end='2026-03-10', dates=dates, estimate={'requests': 10}, now='t0')
        queue.begin(queue.next_pending(), 'job-1', 't1')
        self.assertEqual(queue.public()['current'], '2026-02-01')
        again = Backfill(temp.name)
        self.assertEqual((again.data['status'], again.data['dates'][0]['status']), ('paused', 'pending'))
        again.resume('t2')
        again.begin(again.next_pending(), 'job-2', 't3')
        again.record('2026-02-01', {'status': 'complete', 'message': 'ok', 'details': {'price_as_of': '2026-01-30', 'usable_count': 9}}, 't4')
        self.assertEqual((again.data['status'], again.data['dates'][0]['price_as_of']), ('finished', '2026-01-30'))
        self.assertFalse(again.active)
        self.assertEqual(Backfill(temp.name).public()['counts'], {'done': 1, 'skipped': 1})


class BackfillWorkbenchTests(unittest.TestCase):
    def setUp(self):
        import shutil
        from copy import deepcopy
        from app.workbench import PortalService
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        source_root = Path(__file__).resolve().parents[1]
        for folder in ('configs', 'strategies', 'framework'):
            shutil.copytree(source_root / folder, self.root / folder, ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('data', 'brokers'):
            shutil.copytree(source_root / folder / 'plugins', self.root / folder / 'plugins')
        for target in ('app.assembly.ROOT',):
            patcher = patch(target, self.root); patcher.start(); self.addCleanup(patcher.stop)
        configured = patch('data.massive.configuration_status',
                           return_value={'configured': True, 'message': '', 'network_requested': True})
        configured.start(); self.addCleanup(configured.stop)
        self.calls, self.hold = [], None
        self.response = {'status': 'complete', 'message': '已回填', 'details': {'price_as_of': '2026-01-30', 'usable_count': 9, 'network_requests': 40}}
        def worker(root, action, *args, **kwargs):
            self.calls.append((action, kwargs))
            if self.hold:
                self.hold.wait(3)
            return deepcopy(self.response)
        self.service = PortalService(self.root, worker=worker)
        self.service.source = 'massive'
        self.broker = self.service.registry.default('broker')
        self.addCleanup(lambda: self.service.thread and self.service.thread.join(4))

    def finish(self):
        self.service.thread.join(4)
        self.assertIsNone(self.service.active)

    def test_queue_runs_one_date_per_idle_poll_pauses_on_failure_and_resumes(self):
        from app.workbench import PortalError
        value = {'start': '2026-01-15', 'end': '2026-04-10', 'cadence': 'monthly'}
        plan = self.service.backfill_plan(value)
        self.assertEqual([item['as_of'] for item in plan['dates']], ['2026-02-01', '2026-03-01', '2026-04-01'])
        self.assertEqual((plan['pending_count'], plan['skipped_count']), (3, 0))
        self.assertGreater(plan['estimate']['requests'], 0)
        self.assertTrue(plan['estimate']['income_required'])
        self.assertTrue(self.service.state()['actions']['backfill']['enabled'])
        queue = self.service.backfill_start(value)
        self.assertEqual(queue['status'], 'running')
        with self.assertRaises(PortalError):
            self.service.start('backfill')  # the queue supplies the date; the generic job route cannot
        import threading
        self.hold = threading.Event()
        self.service.backfill_poll()
        self.assertEqual(self.service.active['action'], 'backfill')
        self.assertEqual(self.calls[-1][1]['as_of'], '2026-02-01')
        self.service.backfill_poll()  # busy: nothing else starts
        self.assertEqual(len(self.calls), 1)
        self.hold.set()
        self.finish()
        self.hold = None
        public = self.service.backfill.public()
        self.assertEqual((public['status'], public['dates'][0]['status'], public['dates'][0]['price_as_of']),
                         ('running', 'done', '2026-01-30'))
        self.response = {'status': 'blocked', 'message': '数据权限不足'}
        self.service.backfill_poll(); self.finish()
        public = self.service.backfill.public()
        self.assertEqual((public['status'], public['dates'][1]['status']), ('paused', 'failed'))
        self.assertIn('数据权限不足', public['message'])
        self.service.backfill_poll()
        self.assertIsNone(self.service.active)
        self.response = {'status': 'complete', 'message': '已回填', 'details': {'price_as_of': '2026-02-27', 'usable_count': 9}}
        self.service.backfill_control('resume')
        self.service.backfill_poll(); self.finish()
        self.assertEqual(self.service.backfill.public()['dates'][1]['status'], 'done')
        self.assertEqual(self.service.state()['backfill']['counts'], {'done': 2, 'pending': 1})
        self.service.backfill_control('cancel')
        public = self.service.backfill.public()
        self.assertEqual((public['status'], public['dates'][2]['status']), ('cancelled', 'cancelled'))
        with self.assertRaises(PortalError):
            self.service.backfill_control('resume')
        self.assertTrue((self.root / 'runs/portal/backfill.json').is_file())
        jobs = [job for job in self.service.jobs if job['action'] == 'backfill']
        self.assertEqual([job['as_of'] for job in jobs], ['2026-03-01', '2026-03-01', '2026-02-01'])
        self.assertIsNone(self.service.snapshot())  # backfilled data never becomes the current snapshot

    def test_a_changed_strategy_pauses_the_queue_instead_of_collecting_the_wrong_request(self):
        self.service.backfill_start({'start': '2026-01-15', 'end': '2026-02-10', 'cadence': 'monthly'})
        self.service.backfill.data['request_id'] = 'another-request'
        self.service.backfill.persist()
        self.service.backfill_poll()
        self.assertIsNone(self.service.active)
        self.assertEqual(self.service.backfill.public()['status'], 'paused')
        self.assertEqual(self.calls, [])

    def test_worker_backfill_writes_a_dated_snapshot_under_the_request_directory(self):
        from datetime import date
        from app.control_contracts import ControlError
        from app.portal_worker import perform
        from data.massive_provider import MassiveData
        seen = {}
        def fake_collect(collector, out, *, price_bars=None, progress=None, as_of=None):
            seen.update(as_of=as_of, price_bars=price_bars, out=Path(out))
            Path(out).mkdir(parents=True)
            path = Path(out) / 'preview-data.json'
            path.write_text(json.dumps({'price_as_of': str(as_of - timedelta(days=1)), 'stocks': [{}, {}, {}], 'candidate_count': 5,
                                        'collection_complete': True, 'network_requests': 40, 'cache_hits': 2}), encoding='utf-8')
            return path
        with patch.object(MassiveData, 'collect', fake_collect):
            result = perform('backfill', root=self.root, broker_id=self.broker, source_id='massive', as_of='2026-03-01')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['details'], {'as_of': '2026-03-01', 'price_as_of': '2026-02-28', 'usable_count': 3, 'candidate_count': 5,
                                             'collection_complete': True, 'network_requests': 40, 'cache_hits': 2})
        self.assertEqual(seen['as_of'], date(2026, 3, 1))
        self.assertGreaterEqual(seen['price_bars'], 21)
        self.assertTrue(seen['out'].name.startswith('backfill-2026-03-01-'))
        self.assertTrue(seen['out'].is_relative_to(self.root / 'runs/data/massive/requests'))
        with self.assertRaisesRegex(ControlError, '回填日期'):
            perform('backfill', root=self.root, broker_id=self.broker, source_id='massive', as_of='not-a-date')


if __name__ == '__main__':
    unittest.main()
