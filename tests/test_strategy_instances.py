"""Shared-account ownership, lifecycle, isolated selection and owner-only APIs."""
from copy import deepcopy
from contextlib import contextmanager, closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from app.automation import save
from app.control_contracts import ControlError
from app.module_registry import ModuleRegistry
from app.strategy_instances import StrategyInstances, managed_guard, require_unmanaged
from app.strategy_worker import ManagedRunner, SCHEMA, selection, run_cycle
from app.control_contracts import SnapshotInput
from framework.contracts import OrderState
from framework.journal import Journal
from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS
from app.workbench import PortalService
from framework.configuration import normalize_config
from framework.portfolio import StockSnapshot, StockTarget, EquitySnapshot
from framework.runtime import MarketSnapshot
from framework.strategy_book import owner_plan, reconcile_ownership, settle_ownership
from framework.trading import Account, Quote, ExecutionPolicy, Fees, plan_orders, check_order
from portal.app import create_app

ROOT = Path(__file__).resolve().parents[1]


class OwnershipTests(unittest.TestCase):
    def test_only_due_owners_resize_and_external_shares_are_preserved(self):
        old = {'A': {'XYZ': '10'}, 'B': {'XYZ': '6', 'ZZZ': '2'}}
        planned, net = owner_plan(old, {'EXT': '3'}, {'A': {'XYZ': .6}}, {'A': '.5'}, D('1000'), {'XYZ': D('10')})
        self.assertEqual(planned['A'], {'XYZ': '30'})
        self.assertEqual(planned['B'], old['B'])
        self.assertEqual(net, {'EXT': '3', 'XYZ': '36', 'ZZZ': '2'})

    def test_opposing_owner_changes_become_one_net_account_target(self):
        planned, net = owner_plan({'A': {'XYZ': '5'}, 'B': {'XYZ': '25'}}, {},
                                 {'A': {'XYZ': .5}, 'B': {'XYZ': .1}}, {'A': '.5', 'B': '.5'}, D('1000'), {'XYZ': D('10')})
        self.assertEqual(planned, {'A': {'XYZ': '25'}, 'B': {'XYZ': '5'}})
        self.assertEqual(net, {'XYZ': '30'})

    def test_cash_constrained_fill_allocation_keeps_other_owners_exact(self):
        actual = settle_ownership({'A': {'XYZ': '30'}, 'B': {'XYZ': '6'}},
                                 {'A': {}, 'B': {'XYZ': '6'}}, {}, {'XYZ': '34'}, ['A'])
        self.assertEqual(actual, {'A': {'XYZ': '28'}, 'B': {'XYZ': '6'}})

    def test_shared_symbol_rounding_is_deterministic_and_conserves_shares(self):
        actual = settle_ownership({'A': {'XYZ': '5'}, 'B': {'XYZ': '5'}}, {}, {}, {'XYZ': '7'}, ['B', 'A'])
        self.assertEqual(actual, {'B': {'XYZ': '3'}, 'A': {'XYZ': '4'}})

    def test_unexplained_loss_of_attributed_shares_blocks(self):
        with self.assertRaises(ValueError):
            reconcile_ownership({'A': {'XYZ': '10'}}, {'XYZ': '9'})
        self.assertEqual(reconcile_ownership({'A': {'XYZ': '10'}}, {'XYZ': '13'}), {'XYZ': '3'})

    def test_settlement_cannot_take_stopped_or_external_shares(self):
        with self.assertRaises(ValueError):
            settle_ownership({'A': {}, 'B': {'XYZ': '6'}}, {'B': {'XYZ': '6'}}, {'XYZ': '2'}, {'XYZ': '7'}, ['A'])

    def test_explicit_partial_closure_preserves_unfilled_reductions(self):
        result = settle_ownership({'A': {}}, {'A': {'XYZ': '10'}}, {}, {'XYZ': '4'}, ['A'], partial=True)
        self.assertEqual(result, {'A': {'XYZ': '4'}})


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        for name in ('app', 'configs', 'strategies', 'framework'):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('brokers', 'data'):
            shutil.copytree(ROOT / name / 'plugins', self.root / name / 'plugins')
        self.patch = patch('app.assembly.ROOT', self.root)
        self.patch.start()
        self.registry = ModuleRegistry(self.root)
        self.clock = datetime(2026, 10, 5, 13, 40, tzinfo=timezone.utc)
        self.manager = StrategyInstances(self.root, self.registry, clock=lambda: self.clock)
        self.strategy = json.loads((ROOT / 'tests/fixtures/portfolio_default.json').read_text(encoding='utf-8'))['strategy']
        self.broker = self.registry.default('broker')
        self.source = 'massive'

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def create(self, name='First', budget='.5'):
        value = dict(name=name, strategy=deepcopy(self.strategy), budget=budget, broker_id=self.broker,
                     source_id=self.source, schedule={})
        ident = self.manager.create(value)['id']
        return self.manager.get(ident)

    def fake_worker(self, root, action, **kwargs):
        if action == 'calendar':
            days = [self.clock.date() + timedelta(days=i) for i in range(100)]
            return {'status': 'ready', 'sessions': [str(d) for d in days if d.weekday() < 5]}
        if action == 'strategy-check':
            return {'status': 'complete', 'message': 'Checked', 'account_ref': 'fixture-account'}
        if action == 'strategy-refresh':
            path = self.root / 'runs/fixture-data.json'
            save(path, {})
            return {'status': 'complete', 'snapshot_path': str(path)}
        if action == 'strategy-cycle':
            cycle = json.loads(Path(kwargs['cycle_path']).read_text(encoding='utf-8'))
            managed_guard(self.root, cycle, clock=lambda: self.clock)
            return {'status': 'complete', 'message': 'Fixture executed',
                    'book': {'owners': {i: {} for i in cycle['due_ids']}, 'unmanaged': {}, 'account_ref': 'fixture-account'}}
        raise AssertionError(action)

    def test_independent_parameters_and_schedules_do_not_change_global_config(self):
        before = (self.root / 'configs/portfolio_config.json').read_bytes()
        first, second = self.create(), self.create('Second')
        self.assertNotEqual(first['id'], second['id'])
        value = {k: deepcopy(second[k]) for k in ('name', 'strategy', 'budget', 'broker_id', 'source_id', 'schedule')}
        value['schedule']['cadence'] = 'weekly'
        self.manager.update(second['id'], value)
        self.assertEqual(first['schedule']['cadence'], 'monthly')
        self.assertEqual((self.root / 'configs/portfolio_config.json').read_bytes(), before)

    def test_stopping_one_shot_clears_request_before_restart(self):
        item = self.create()
        self.manager.run_once(item['id'])
        self.manager.stop(item['id'])
        self.assertFalse(item['force_run'])
        self.manager.start(item['id'])
        self.assertFalse(item['one_shot'])

    def test_stop_during_network_check_does_not_resume_or_execute(self):
        item = self.create()
        self.manager.start(item['id'])
        started, stopped = threading.Event(), threading.Event()
        def stop():
            started.wait(5)
            with self.manager.lock:
                self.manager.stop(item['id'])
            stopped.set()
        thread = threading.Thread(target=stop)
        thread.start()
        def worker(root, action, **kwargs):
            if action == 'strategy-check':
                started.set()
                self.assertTrue(stopped.wait(5))
            return self.fake_worker(root, action, **kwargs)
        result = self.manager.tick(worker)
        thread.join(5)
        self.assertFalse(item['enabled'])
        self.assertEqual(item['status'], 'stopped')
        self.assertFalse(self.manager.data['pending'])

    def test_complete_managed_cycle_uses_real_preparation_execution_and_journal(self):
        stamp = datetime(2026, 10, 5, 13, 40, tzinfo=timezone.utc)
        self.clock = stamp
        item, held = self.create('Due', '.5'), self.create('Stopped', '.4')
        item['strategy'] = {'type': 'equal_weight', 'count': 1, 'weight': .18}
        self.manager.data['books'][self.broker] = {
            'owners': {held['id']: {'AAA': '6'}}, 'unmanaged': {}, 'account_ref': None}
        self.manager.start(item['id'])
        actual = [Account('fixture-paper', 'USD', D('910'), (('AAA', D('6')), ('EXT', D('3'))), stamp)]
        stock = StockSnapshot('original:AAA', 'AAA', date(2026, 10, 2), date(2026, 10, 2), 'Tech', 1, (10.,)*253, (1e8,)*20)
        sent, saved = [], {}
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None): return stamp if tz else stamp.replace(tzinfo=None)
        class Source:
            directory = self.root / 'runs/data/fixture'
            def validate_request(self): pass
            def collect(self, target, **kwargs):
                path = Path(str(target) + '.json'); save(path, {'fixture': True}); return path
            def load(self, raw):
                return SnapshotInput('fixture-input', True, raw, (stock,), {}, {}, order_requirements=US_EQUITY_LIMIT_ORDERS)
            def paper_signal(self, raw, clock): return (stock,), []
        source = Source()
        registry = self.registry
        entry = registry.catalog('broker')[registry.descriptor('broker', self.broker)['type']]
        test = self
        class Control:
            def observe_account(self): return actual[0]
            def observe_quotes(self, mapping, max_age):
                return {s: Quote(s, D(10), D(10), stamp) for s in mapping}, date(2026, 10, 2), stamp
            @contextmanager
            def session(self, cfg, snapshot, fees):
                class Broker:
                    mode = 'broker_paper'
                    def account(self):
                        reverse = {ticker: asset for asset, ticker in snapshot.instruments.items()}
                        return replace(actual[0], positions=tuple(sorted((reverse[t], q) for t, q in actual[0].positions)))
                    def execution_time(self): return stamp
                    def open_orders(self): return ()
                    def query(self, ident): return saved[ident]
                    def submit(self, order):
                        sent.append(order)
                        mapped = check_order(order, self.account(), snapshot.quotes, stamp,
                                             test_runner_policy(cfg), fees)
                        actual[0] = replace(mapped, positions=tuple((snapshot.instruments[s], q) for s, q in mapped.positions))
                        saved[order.client_id] = OrderState(order.client_id, 'fixture-' + order.client_id,
                                                           'filled', order.quantity, order.limit_price)
                        return saved[order.client_id]
                with closing(Journal(test.root / 'runs/fixture-journal.sqlite')) as journal:
                    yield Broker(), journal
        from framework.configuration import policy_from_config as test_runner_policy
        control = Control(); control.entry = entry
        def worker(root, action, **kwargs):
            if action != 'strategy-cycle': return self.fake_worker(root, action, **kwargs)
            with patch('app.strategy_worker.ModuleRegistry', return_value=registry), \
                 patch.object(registry, 'control', side_effect=lambda kind, *a, **k: control if kind == 'broker' else source), \
                 patch('app.strategy_worker.datetime', FixedDateTime), \
                 patch('app.paper_preparation.datetime', FixedDateTime), \
                 patch('app.strategy_worker.managed_guard', side_effect=lambda r, c: managed_guard(r, c, clock=lambda: stamp)):
                return run_cycle(root, kwargs['cycle_path'], lambda message: None)
        result = self.manager.tick(worker)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual([(o.side, o.quantity) for o in sent], [('BUY', D(9))])
        book = self.manager.data['books'][self.broker]
        self.assertEqual(book['owners'][held['id']], {'AAA': '6'})
        self.assertEqual(book['owners'][item['id']], {'AAA': '9'})
        self.assertEqual(book['unmanaged'], {'EXT': '3'})
        self.manager.tick(self.fake_worker)
        self.assertEqual(len(sent), 1)

    def test_overbooked_capital_is_rejected_even_after_owner_is_stopped(self):
        first, second = self.create(budget='.7'), self.create('Second', '.4')
        self.manager.start(first['id'])
        with self.assertRaises(ControlError):
            self.manager.start(second['id'])
        self.manager.data['books'][self.broker] = {'owners': {first['id']: {'XYZ': '5'}}}
        self.manager.stop(first['id'])
        with self.assertRaises(ControlError):
            self.manager.start(second['id'])

    def test_reading_pending_state_and_legacy_guard_do_not_revoke_live_batch(self):
        item = self.create(); self.manager.start(item['id'])
        path = self.root / 'runs/strategy_manager/cycles/cycle-live.json'
        save(path, {'execution_attempted': True})
        self.manager.data['pending'][self.broker] = str(path); self.manager.persist()
        before = self.manager.path.read_bytes()
        reader = StrategyInstances(self.root, self.registry)
        self.assertTrue(reader.get(item['id'])['enabled'])
        with self.assertRaises(ControlError):
            require_unmanaged(self.root, self.registry, self.broker)
        self.assertEqual(self.manager.path.read_bytes(), before)

    def test_stopped_owner_cannot_increase_reserved_budget_past_account_limit(self):
        first, second = self.create(budget='.5'), self.create('Second', '.4')
        self.manager.data['books'][self.broker] = {'owners': {first['id']: {'XYZ': '5'}}}
        self.manager.start(second['id'])
        value = {k: deepcopy(first[k]) for k in ('name','strategy','broker_id','source_id','budget','schedule')}
        value['budget'] = '.7'
        before = self.manager.path.read_bytes()
        with self.assertRaisesRegex(ControlError, '100%'):
            self.manager.update(first['id'], value)
        self.assertEqual(first['budget'], '0.5')
        self.assertEqual(self.manager.path.read_bytes(), before)

    def test_read_only_check_failure_does_not_stop_other_instances(self):
        first, second = self.create(), self.create('Second')
        self.manager.start(first['id']); self.manager.start(second['id'])
        def worker(*args, **kwargs): raise RuntimeError('Fixture read-only failure')
        service = PortalService(self.root, worker=worker)
        with service.lock:
            service.strategy_instance_job(first['id'], 'strategy-check')
        service.thread.join(5)
        self.assertFalse(service.thread.is_alive())
        self.assertTrue(service.strategy_instances.get(first['id'])['enabled'])
        self.assertTrue(service.strategy_instances.get(second['id'])['enabled'])
        self.assertEqual(service.jobs[0]['status'], 'blocked')

    def test_account_overview_counts_running_and_stopped_ownership_but_not_empty_drafts(self):
        held, running, draft = self.create(budget='.3'), self.create('Running', '.4'), self.create('Draft', '.8')
        self.manager.data['books'][self.broker] = {'owners': {held['id']: {'AAA':'6'}}}
        self.manager.start(running['id'])
        account = self.manager.public()['accounts'][0]
        self.assertEqual(D(account['reserved_budget']), D('.7'))
        self.assertEqual(D(account['available_budget']), D('.3'))
        self.assertEqual(account['running_count'], 1)
        self.assertEqual(account['stopped_holding_count'], 1)
        self.assertEqual(account['instance_count'], 3)

    def test_preview_revision_changes_when_confirmed_holdings_or_code_change(self):
        item = self.create()
        before = self.manager.public()['instances'][0]['revision']
        self.manager.data['books'][self.broker] = {'owners': {item['id']: {'AAA':'6'}}}
        held = self.manager.public()['instances'][0]['revision']
        self.assertNotEqual(before, held)
        (self.root/'strategies/equal_weight.py').write_text('# fixture changed')
        self.assertNotEqual(held, self.manager.public()['instances'][0]['revision'])

    def test_equal_data_requirements_share_one_refresh_but_different_requirements_do_not(self):
        first, second = self.create(), self.create('Second')
        self.manager.start(first['id']); self.manager.start(second['id'])
        calls = []
        def worker(root, action, **kwargs):
            calls.append(action); return self.fake_worker(root, action, **kwargs)
        self.manager.tick(worker)
        self.assertEqual(calls.count('strategy-refresh'), 1)
        self.assertEqual(first['prepared'], second['prepared'])
        self.assertEqual(calls.count('strategy-cycle'), 1)
        self.manager.stop(first['id']); self.manager.stop(second['id'])
        first.pop('prepared'); second.pop('prepared')
        second['strategy']['lookback'] = second['strategy'].get('lookback', 252) + 1
        self.manager.start(first['id']); self.manager.start(second['id'])
        calls.clear(); self.manager.tick(worker)
        self.assertEqual(calls.count('strategy-refresh'), 2)

    def test_execution_exception_pauses_only_the_active_account(self):
        first, second = self.create(), self.create('Other account')
        second['broker_id'] = next(x['id'] for x in self.registry.descriptors('broker')
            if x['enabled'] and x['mode'] == 'broker_paper' and x['id'] != self.broker)
        self.manager.start(first['id']); self.manager.start(second['id'])
        def worker(*args, **kwargs): raise RuntimeError('Fixture account failure')
        service = PortalService(self.root, worker=worker)
        with service.lock: service._managed_job('strategy-cycle')
        service.thread.join(5)
        self.assertFalse(service.thread.is_alive())
        self.assertFalse(service.strategy_instances.get(first['id'])['enabled'])
        self.assertTrue(service.strategy_instances.get(second['id'])['enabled'])

    def preview_fixture(self):
        item = self.create()
        item['strategy'] = {'type':'equal_weight', 'count':1, 'weight':.18}
        self.manager.persist()
        stock = StockSnapshot('fixture:AAA', 'AAA', date(2026,10,2), date(2026,10,2), 'Tech', 1,
                              (10.,)*253, (1e8,)*20)
        class Source:
            directory = self.root/'runs/data/preview'
            def validate_request(self): pass
            def load(self, raw): return SnapshotInput('fixture-input', True, raw, (stock,), {}, {})
            def collect(self, *a, **k): raise AssertionError('Preview must not download')
            def inspect(self, *a, **k): raise AssertionError('Preview must not inspect live permissions')
            def paper_signal(self, *a, **k): raise AssertionError('Preview must not prepare trading data')
        source = Source()
        save(source.directory/'snapshot.json', {'fixture':True})
        save(source.directory/'latest.json', {'path':str(source.directory/'snapshot.json')})
        packet = dict(deepcopy(item), _held_positions={}, _selection_timeout_seconds=10)
        return item, source, packet

    def test_cached_preview_runs_isolated_selection_off_hours_without_broker_or_state_changes(self):
        from app.strategy_preview import preview
        item, source, packet = self.preview_fixture()
        before = self.manager.path.read_bytes()
        calls = []
        def control(kind, *args, **kwargs):
            calls.append(kind); self.assertEqual(kind, 'data'); return source
        with patch('app.strategy_preview.ModuleRegistry', return_value=self.registry), \
             patch.object(self.registry, 'control', side_effect=control):
            result = preview(self.root, packet, lambda message: None,
                             clock=datetime(2026,10,10,13,40,tzinfo=timezone.utc))
        self.assertEqual(set(calls), {'data'})
        self.assertTrue(result['research_only']); self.assertFalse(result['execution_ready'])
        self.assertEqual(result['targets'][0]['ticker'], 'AAA')
        self.assertEqual(D(str(result['targets'][0]['account_weight'])), D('.09'))
        self.assertEqual(result['instance_revision'], self.manager.public()['instances'][0]['revision'])
        self.assertEqual(self.manager.path.read_bytes(), before)
        self.assertFalse(item['enabled'])

    def test_cached_preview_missing_data_or_escaped_pointer_blocks_without_download(self):
        from app.strategy_preview import preview
        item, source, packet = self.preview_fixture()
        with patch('app.strategy_preview.ModuleRegistry', return_value=self.registry), \
             patch.object(self.registry, 'control', return_value=source):
            (source.directory/'latest.json').unlink()
            with self.assertRaisesRegex(ControlError, '尚无匹配的数据'):
                preview(self.root, packet, lambda message: None, clock=self.clock)
            other = self.root/'runs/data/other/snapshot.json'; save(other, {})
            save(source.directory/'latest.json', {'path':str(other)})
            with self.assertRaisesRegex(ControlError, '不属于当前策略'):
                preview(self.root, packet, lambda message: None, clock=self.clock)

    def test_preview_http_requires_owner_and_accepts_no_order_parameters(self):
        item = self.create()
        service = PortalService(self.root, worker=lambda *a, **k: {'status':'blocked','message':'No fixture data'})
        client = TestClient(create_app(self.root, service=service), base_url='http://127.0.0.1:8765')
        endpoint = f'/api/strategy-instances/{item["id"]}/preview'
        self.assertEqual(client.post(endpoint, json={}).status_code, 403)
        client.headers['X-Portal-Token'] = client.get('/api/session').json()['token']
        self.assertEqual(client.post(endpoint, json={'orders':[]}).status_code, 409)
        self.assertEqual(client.post(endpoint, json={}).status_code, 200)
        service.thread.join(5)
        self.assertEqual(service.jobs[0]['action'], 'strategy-preview')
        self.assertFalse(service.strategy_instances.get(item['id'])['enabled'])
        service.strategy_instances.data['pending'][self.broker] = 'fixture'
        self.assertEqual(client.post(endpoint, json={}).status_code, 409)
        client.close()

    def test_read_only_preview_failure_does_not_pause_running_instances(self):
        item = self.create(); self.manager.start(item['id'])
        from app.strategy_worker import dispatch
        with patch('app.strategy_preview.preview', side_effect=RuntimeError('Fixture preview error')):
            result = dispatch(self.root, 'strategy-preview', instance=item)
        self.assertEqual(result['status'], 'blocked')
        service = PortalService(self.root, worker=lambda *a, **k: result)
        with service.lock: service.strategy_instance_job(item['id'], 'strategy-preview')
        service.thread.join(5)
        self.assertTrue(service.strategy_instances.get(item['id'])['enabled'])
        self.assertEqual(service.jobs[0]['status'], 'blocked')

    def test_shared_account_runs_one_batch_and_restart_does_not_repeat_period(self):
        first, second = self.create(), self.create('Second')
        self.manager.start(first['id']); self.manager.start(second['id'])
        calls = []
        def worker(root, action, **kwargs):
            calls.append(action)
            return self.fake_worker(root, action, **kwargs)
        self.assertEqual(self.manager.tick(worker)['status'], 'complete')
        self.assertEqual(calls.count('strategy-cycle'), 1)
        restored = StrategyInstances(self.root, self.registry, clock=lambda: self.clock)
        restored.tick(worker)
        self.assertEqual(calls.count('strategy-cycle'), 1)
        self.assertTrue(all(x['initialized'] for x in restored.data['instances']))

    def test_stop_revokes_a_frozen_batch_before_new_submission(self):
        item = self.create(); self.manager.start(item['id'])
        def worker(root, action, **kwargs):
            if action == 'strategy-cycle':
                cycle = json.loads(Path(kwargs['cycle_path']).read_text(encoding='utf-8'))
                self.manager.stop(item['id'])
                with self.assertRaises(ControlError):
                    managed_guard(root, cycle, clock=lambda: self.clock)
                return {'status': 'paused', 'message': 'Stopped'}
            return self.fake_worker(root, action, **kwargs)
        self.manager.tick(worker)
        self.assertFalse(item['enabled'])
        self.assertFalse(self.manager.data['pending'])

    def test_interrupted_submitted_batch_survives_and_pauses_restart(self):
        item = self.create(); self.manager.start(item['id'])
        path = self.root / 'runs/strategy_manager/cycles/cycle-interrupted.json'
        save(path, {'execution_attempted': True})
        self.manager.data['pending'][self.broker] = str(path); self.manager.persist()
        restored = StrategyInstances(self.root, self.registry, recover_pending=True)
        self.assertFalse(restored.get(item['id'])['enabled'])
        self.assertEqual(restored.get(item['id'])['status'], 'paused')
        self.assertEqual(restored.pending_cycle(item['id']), {'execution_attempted': True})
        with self.assertRaises(ControlError):
            restored.start(item['id'])

    def test_one_shot_returns_to_stopped_after_one_batch(self):
        item = self.create()
        self.manager.run_once(item['id']); self.manager.tick(self.fake_worker)
        self.assertFalse(item['enabled'])
        self.assertEqual(item['status'], 'stopped')
        self.assertIsNotNone(item['last_run'])

    def test_closed_market_sets_future_run_without_executing(self):
        self.clock = datetime(2026, 10, 10, 13, 40, tzinfo=timezone.utc)
        item = self.create(); self.manager.start(item['id'])
        calls = []
        def worker(root, action, **kwargs):
            calls.append(action); return self.fake_worker(root, action, **kwargs)
        self.manager.tick(worker)
        self.assertNotIn('strategy-cycle', calls)
        self.assertNotIn('strategy-refresh', calls)
        self.assertIn('2026-10-12', item['next_run'])

    def test_code_change_pauses_bound_instances(self):
        item = self.create(); self.manager.start(item['id'])
        (self.root / 'strategies/new_file.py').write_text('# changed')
        result = self.manager.tick(self.fake_worker)
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(item['enabled'])

    def test_running_instance_cannot_be_edited_or_deleted(self):
        item = self.create(); self.manager.start(item['id'])
        with self.assertRaises(ControlError):
            self.manager.update(item['id'], {})
        with self.assertRaises(ControlError):
            self.manager.remove(item['id'])

    def test_invalid_state_is_fail_closed_and_never_overwritten(self):
        path = self.root / 'runs/strategy_manager/state.json'; path.parent.mkdir(parents=True)
        path.write_text('{broken')
        manager = StrategyInstances(self.root, self.registry)
        self.assertTrue(manager.public()['corrupt']); self.assertFalse(manager.due())
        with self.assertRaises(ControlError):
            manager.persist()
        self.assertEqual(path.read_text(), '{broken')

    def test_selection_runs_in_an_actual_separate_process(self):
        item = {'name': 'Control', 'strategy': self.strategy}
        targets = selection(self.root, item, (), self.clock.date(), (), ExecutionPolicy(), 10)
        self.assertEqual(targets, ())

    def test_momentum_selection_transports_missing_optional_fundamentals(self):
        item = {'name': 'MOM8', 'strategy': {'type': 'mom8'}}
        stock = EquitySnapshot('fixture:AAA', 'AAA', date(2026,10,2), date(2026,10,2), 'Tech', float('nan'),
            tuple(100.+i for i in range(253)), (), 3e9, 352., (2e6,)*20)
        targets = selection(self.root, item, (stock,), date(2026,10,5), (), ExecutionPolicy(), 10)
        self.assertEqual([t.asset_id for t in targets], ['fixture:AAA'])

    def test_selection_timeout_terminates_only_the_calculation_process(self):
        path = self.root / 'strategies/slow_fixture.py'
        path.write_text('''from framework.portfolio import HistoryRequirements\nimport time\nclass Slow:\n history_requirements=HistoryRequirements(1,1)\n def select(self, stocks):\n  time.sleep(5)\n  return ()\n''')
        save(self.root / 'strategies/plugins/slow_fixture.json',
             {'type': 'slow_fixture', 'kind': 'portfolio', 'entrypoint': 'strategies.slow_fixture:Slow', 'version': '1'})
        with self.assertRaisesRegex(ControlError, '超过 1 秒'):
            selection(self.root, {'name': 'Slow', 'strategy': {'type': 'slow_fixture'}}, (), self.clock.date(), (), ExecutionPolicy(), 1)

    def test_http_mutations_are_authenticated_and_do_not_start_instances_on_creation(self):
        service = PortalService(self.root, worker=lambda *a, **k: {'status': 'complete', 'message': 'Fixture'})
        client = TestClient(create_app(self.root, service=service), base_url='http://127.0.0.1:8765')
        value = dict(name='API', strategy=self.strategy, budget='.5', broker_id=self.broker, source_id=self.source, schedule={})
        self.assertEqual(client.post('/api/strategy-instances', json=value).status_code, 403)
        client.headers['X-Portal-Token'] = client.get('/api/session').json()['token']
        result = client.post('/api/strategy-instances', json=value)
        self.assertEqual(result.status_code, 200)
        ident = result.json()['id']
        self.assertFalse(service.strategy_instances.get(ident)['enabled'])
        self.assertEqual(client.post(f'/api/strategy-instances/{ident}/start', json={}).status_code, 409)
        self.assertEqual(client.post(f'/api/strategy-instances/{ident}/start', json={'confirmation': ident}).status_code, 200)
        self.assertEqual(client.post(f'/api/strategy-instances/{ident}/stop', json={}).status_code, 200)
        client.close()

    def test_strategy_bundle_roundtrip_and_traversal_are_checked(self):
        service = PortalService(self.root)
        original = service.strategy_files.bundle('equal_weight')
        with zipfile.ZipFile(io.BytesIO(original)) as package:
            code = package.read('strategy.py'); entry = json.loads(package.read('manifest.json'))
        entry['type'] = 'uploaded_control'
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as package:
            package.writestr('strategy.py', code); package.writestr('manifest.json', json.dumps(entry))
        uploaded = service.upload_strategy(out.getvalue())
        self.assertEqual(uploaded['type'], 'uploaded_control')
        self.assertIn('uploaded_control', self.registry.catalog('strategy'))
        with self.assertRaises(ControlError):
            service.upload_strategy(out.getvalue())
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, 'w') as package:
            package.writestr('../strategy.py', code); package.writestr('manifest.json', '{}')
        with self.assertRaises(ControlError):
            service.upload_strategy(bad.getvalue())

    def test_online_edit_checks_revision_syntax_and_makes_backup(self):
        service = PortalService(self.root)
        original = service.strategy_source('equal_weight')
        with self.assertRaises(ControlError):
            service.strategy_source('equal_weight', {'content': 'invalid (', 'revision': original['revision']})
        with self.assertRaises(ControlError):
            service.strategy_source('equal_weight', {'content': original['content'], 'revision': 'wrong'})
        result = service.strategy_source('equal_weight', {'content': original['content'] + '\n# edited\n', 'revision': original['revision']})
        self.assertNotEqual(result['revision'], original['revision'])
        backup = list((self.root / 'runs/strategy_manager/source-history').glob('*.py'))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_text(encoding='utf-8'), original['content'])

    def test_online_edit_rejects_context_invalid_python_before_changing_source(self):
        service = PortalService(self.root)
        original = service.strategy_source('equal_weight')
        path = self.root / original['filename']
        before = path.read_bytes()
        with self.assertRaisesRegex(ControlError, 'Python 语法错误'):
            service.strategy_source('equal_weight', {
                'content': original['content'] + '\nreturn 1\n', 'revision': original['revision']})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / 'runs/strategy_manager/source-history').exists())

    def test_online_edit_preserves_utf8_line_endings_without_executing_source(self):
        service = PortalService(self.root)
        original = service.strategy_source('equal_weight')
        path = self.root / original['filename']
        normalized = original['content'].replace('\r\n', '\n')
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                initial = normalized.replace('\n', newline).encode('utf-8')
                path.write_bytes(initial)
                opened = service.strategy_source('equal_weight')
                content = opened['content'] + newline + '# 策略代码' + newline + "raise RuntimeError('must not execute')" + newline
                result = service.strategy_source('equal_weight', {'content': content, 'revision': opened['revision']})
                self.assertEqual(path.read_bytes(), content.encode('utf-8'))
                reopened = service.strategy_source('equal_weight')
                self.assertEqual(reopened['content'], content)
                self.assertEqual(reopened['revision'], result['revision'])
        backups = [item.read_bytes() for item in (self.root / 'runs/strategy_manager/source-history').glob('*.py')]
        self.assertCountEqual(backups, [normalized.replace('\n', newline).encode('utf-8') for newline in ('\n', '\r\n')])

    def test_strategy_upload_preserves_line_endings_and_validates_without_execution(self):
        service = PortalService(self.root)
        with zipfile.ZipFile(io.BytesIO(service.strategy_files.bundle('equal_weight'))) as package:
            template = json.loads(package.read('manifest.json'))
            original = package.read('strategy.py').decode('utf-8-sig').replace('\r\n', '\n')
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                entry = dict(template, type='uploaded_lf' if newline == '\n' else 'uploaded_crlf')
                content = (original + "\n# 策略代码\nraise RuntimeError('must not execute')\n").replace('\n', newline)
                out = io.BytesIO()
                with zipfile.ZipFile(out, 'w') as package:
                    package.writestr('strategy.py', content.encode('utf-8'))
                    package.writestr('manifest.json', json.dumps(entry))
                result = service.upload_strategy(out.getvalue())
                opened = service.strategy_source(result['type'])
                self.assertEqual((self.root / opened['filename']).read_bytes(), content.encode('utf-8'))
                self.assertEqual(opened['content'], content)
        invalid = io.BytesIO()
        with zipfile.ZipFile(invalid, 'w') as package:
            package.writestr('strategy.py', original + '\nreturn 1\n')
            package.writestr('manifest.json', json.dumps(dict(template, type='uploaded_invalid')))
        with self.assertRaisesRegex(ControlError, 'Python 语法错误'):
            service.upload_strategy(invalid.getvalue())
        self.assertNotIn('uploaded_invalid', self.registry.catalog('strategy'))
        self.assertFalse((self.root / 'strategies/plugins/uploaded_invalid.json').exists())

    def test_strategy_source_changes_require_readable_control_state(self):
        for folder, attribute in (('automation', 'automation'), ('strategy_manager', 'strategy_instances')):
            with self.subTest(state=folder):
                state_path = self.root / 'runs' / folder / 'state.json'
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text('{broken', encoding='utf-8')
                service = PortalService(self.root)
                self.assertTrue(getattr(service, attribute).corrupt)
                original = service.strategy_source('equal_weight')
                source_path = self.root / original['filename']
                before = source_path.read_bytes()
                with self.assertRaisesRegex(ControlError, '状态无法核对'):
                    service.strategy_source('equal_weight', {
                        'content': original['content'] + '\n# changed\n', 'revision': original['revision']})
                with self.assertRaisesRegex(ControlError, '状态无法核对'):
                    service.upload_strategy(service.strategy_files.bundle('equal_weight'))
                self.assertEqual(source_path.read_bytes(), before)
                self.assertEqual(state_path.read_text(encoding='utf-8'), '{broken')
                self.assertFalse((self.root / 'runs/strategy_manager/source-history').exists())
                state_path.unlink()


class CombinedRunnerTests(unittest.TestCase):
    def test_net_plan_and_frozen_integer_positions_survive_quote_refresh(self):
        stamp = datetime(2026, 10, 5, 14, tzinfo=timezone.utc)
        stock = StockSnapshot('MANAGED:US:AAA', 'AAA', date(2026, 10, 2), date(2026, 10, 2), 'Tech', 1, (10.,), (10.,))
        source = SimpleNamespace(parts={'A': {'stocks': (stock,)}}, rows=(stock,))
        cycle = {'id': 'fixture', 'due_ids': ['A'], 'limits': {'selection_timeout_seconds': 10},
                 'instances': [{'id': 'A', 'name': 'A', 'budget': '.5', 'strategy': {}, 'generation': 'a'},
                               {'id': 'B', 'name': 'B', 'budget': '.5', 'strategy': {}, 'generation': 'b'}],
                 'book': {'owners': {'B': {'AAA': '6'}}, 'unmanaged': {}, 'account_ref': None}}
        cfg = json.loads((ROOT / 'tests/fixtures/portfolio_default.json').read_text(encoding='utf-8'))
        cfg['limits']['max_weight'] = '.7'
        account = Account('paper', 'USD', D('860'), (('AAA', D('6')), ('CCC', D('4'))), stamp)
        selector = lambda *args: (StockTarget('MANAGED:US:AAA', .6, 1.),)
        runner = ManagedRunner(ROOT, source, cycle, normalize_config(cfg), account=account, selector=selector)
        quotes = {'MANAGED:US:AAA': Quote('MANAGED:US:AAA', D('10'), D('10'), stamp),
                  'HELD:US:CCC': Quote('HELD:US:CCC', D('20'), D('20'), stamp)}
        snapshot = MarketSnapshot(SCHEMA, (stock,), quotes, stamp, instruments={'MANAGED:US:AAA': 'AAA', 'HELD:US:CCC': 'CCC'})
        decision = runner.plan(snapshot)
        self.assertEqual(decision.record['desired_positions'], {'MANAGED:US:AAA': '36', 'HELD:US:CCC': '4'})
        changed = dict(quotes, **{'MANAGED:US:AAA': replace(quotes['MANAGED:US:AAA'], ask=D('10.01'))})
        self.assertEqual(runner.plan(replace(snapshot, quotes=changed)), decision)
        mapped_account = replace(account, positions=(('MANAGED:US:AAA', D('6')), ('HELD:US:CCC', D('4'))))
        orders = plan_orders(decision, mapped_account, quotes, stamp, runner.policy, Fees())
        self.assertEqual([(o.symbol, o.side, o.quantity) for o in orders], [('MANAGED:US:AAA', 'BUY', D('30'))])
