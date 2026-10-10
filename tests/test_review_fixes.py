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


if __name__ == '__main__':
    unittest.main()
