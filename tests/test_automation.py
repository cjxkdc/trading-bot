"""Offline schedule/crash/revocation tests. No network or real order transmission."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.automation import Automation, code_revision, submission_guard
from app.control_contracts import ControlError
from framework.scheduling import schedule_config, month_window, period_window, next_window, initial_window, DEFAULT_SCHEDULE

NY = ZoneInfo('America/New_York')


class Registry:
    def __init__(self, root):
        self.root, self.context_value, self.mode = root, 'context-one', 'broker_paper'
    def descriptor(self, kind, ident):
        return dict(enabled=True, mode=self.mode, name=ident, capabilities=(
            ['inspect','prepare','execute','calendar','reconcile'] if kind=='broker' else ['collect','paper_signal']))
    def configuration(self, ident): return {'strategy':{'type':'test_strategy'}}
    def catalog(self, kind): return {'test_strategy':{'type':'test_strategy','display_name':'Test strategy'}}
    def compatible(self, cfg, source): return None
    def context(self, broker, source): return self.context_value
    def connection_context(self, broker): return 'connection'
    def control(self, kind, ident):
        return SimpleNamespace(directory=self.root/'runs/data/example', configuration_status=lambda:{'configured':True})


class ScheduleTests(unittest.TestCase):
    def test_daily_skips_holidays_and_uses_each_session(self):
        cfg = schedule_config({'cadence':'daily'})
        now = datetime(2026,10,2,12,tzinfo=NY)
        start = next_window(now, [date(2026,10,2),date(2026,10,5)], cfg)
        self.assertEqual(start.date(), date(2026,10,5))

    def test_weekly_holiday_rollover_keeps_original_period(self):
        cfg = schedule_config({'cadence':'weekly','weekday':4})
        sessions = [date(2026,10,1),date(2026,10,5),date(2026,10,9),date(2026,10,12)]
        start, end, key = period_window(date(2026,10,5), sessions, cfg)
        self.assertEqual(start.date(), date(2026,10,5))
        self.assertEqual(key, 'week-2026-09-28')
        self.assertEqual(next_window(end, sessions, cfg).date(), date(2026,10,9))

    def test_initial_window_can_start_midmonth_or_wait_through_weekend(self):
        sessions = [date(2026,10,2),date(2026,10,5)]
        now = datetime(2026,10,2,12,tzinfo=NY)
        self.assertEqual(initial_window(now, sessions, DEFAULT_SCHEDULE)[0], now)
        for now in (datetime(2026,10,2,15,58,tzinfo=NY), datetime(2026,10,3,12,tzinfo=NY)):
            self.assertEqual(initial_window(now, sessions, DEFAULT_SCHEDULE)[0].date(), date(2026,10,5))

    def test_exchange_holiday_and_weekend_are_taken_from_supplied_calendar(self):
        sessions=[date(2026,1,2),date(2026,1,5)]
        start,end=month_window(date(2026,1,1),sessions,DEFAULT_SCHEDULE)
        self.assertEqual(start.date(),date(2026,1,2))
        self.assertEqual(end-start,timedelta(hours=1))
        cfg=schedule_config({'day':3})
        start,_=month_window(date(2026,10,1),[date(2026,10,1),date(2026,10,5)],cfg)
        self.assertEqual(start.date(),date(2026,10,5))
    def test_dst_changes_utc_time_without_changing_local_market_time(self):
        before,_=month_window(date(2026,3,1),[date(2026,3,2)],DEFAULT_SCHEDULE)
        after,_=month_window(date(2026,4,1),[date(2026,4,1)],DEFAULT_SCHEDULE)
        self.assertEqual(before.astimezone(timezone.utc).hour,14)
        self.assertEqual(after.astimezone(timezone.utc).hour,13)
    def test_missing_calendar_never_falls_back_to_weekdays(self):
        with self.assertRaises(ValueError): month_window(date(2026,1,1),[],DEFAULT_SCHEDULE)
    def test_invalid_or_injected_schedule_rejected(self):
        for bad in ({'day':True},{'day':31},{'trade_time':'25:00'},{'timezone':'invalid/timezone'},
                    {'prepare_time':'12:00'},{'enabled':True},{'poll_seconds':0}):
            with self.subTest(bad=bad),self.assertRaises(ValueError): schedule_config(bad)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.registry=Registry(self.root)
        self.now=datetime(2026,10,1,9,40,tzinfo=NY)
        self.auto=Automation(self.root,self.registry,clock=lambda:self.now)
        folder=self.root/'runs/data/example'
        folder.mkdir(parents=True)
        self.signal=folder/'snapshot.json'
        self.signal.write_text('{}')
        (folder/'latest.json').write_text(json.dumps({'path':str(self.signal)}))
        self.envelope=self.root/'runs/prepared.json'
        self.envelope.write_text('{}')
        self.calls=[]
        self.results={}
    def tearDown(self): self.temp.cleanup()
    def worker(self,root,action,snapshot=None,token=None,progress=None,**kwargs):
        self.calls.append((action,snapshot,token,kwargs))
        if action in self.results:
            result=self.results[action]
            if isinstance(result,BaseException): raise result
            return result
        if action=='calendar': return dict(status='ready',message='calendar',sessions=['2026-10-01','2026-10-02','2026-10-05','2026-11-02','2026-12-01'])
        if action=='status': return dict(status='connected',message='account')
        if action=='refresh': return dict(status='previewed',message='data')
        if action=='prepare': return dict(status='previewed',message='prepared',execution_ready=True,
                                         snapshot_path=str(self.envelope),preview_token='frozen-token')
        return dict(status='complete',message=action,recovery_state='complete')
    def enable(self): self.auto.enable('paper-broker','data-provider',True)
    def next(self): self.now+=timedelta(seconds=31); return self.auto.tick(self.worker)
    def test_open_and_status_are_offline_and_do_not_enable(self):
        self.assertFalse(self.auto.public('paper-broker','data-provider')['enabled'])
        self.assertFalse(self.auto.due())
        self.assertFalse(self.auto.path.exists())
        self.assertIsNone(self.auto.tick(self.worker))
        self.assertEqual(self.calls,[])

    def test_public_binding_keeps_actual_plan_ids_when_other_account_selected(self):
        self.enable()
        original = dict(self.auto.data['binding'])
        original.update(account_id='PRIVATE-ACCOUNT', token='PRIVATE-TOKEN')
        self.auto.data['binding'] = original
        view = self.auto.public('different-broker', 'different-source')
        self.assertEqual(view['binding']['broker_id'], 'paper-broker')
        self.assertEqual(view['binding']['source_id'], 'data-provider')
        self.assertEqual(set(view['binding']), {'strategy_name', 'broker_name', 'source_name', 'broker_id', 'source_id'})
        self.assertNotIn('PRIVATE', json.dumps(view))
        self.assertEqual(self.auto.data['binding'], original)
    def test_enable_requires_verified_paper_account(self):
        with self.assertRaises(ControlError):self.auto.enable('b','s',False)
        self.registry.mode='live'
        with self.assertRaises(ControlError):self.auto.enable('b','s',True)

    def test_startup_performs_live_account_check_before_any_data_or_orders(self):
        self.auto.enable('paper-broker','data-provider',False,initialize=True)
        self.results['status'] = dict(status='blocked',message='OpenD is offline')
        result = self.auto.tick(self.worker)
        self.assertEqual(result['status'], 'paused')
        self.assertEqual([c[0] for c in self.calls], ['calendar','status'])
        self.assertFalse(self.auto.data['enabled'])

    def test_startup_weekend_checks_now_then_builds_next_session_not_next_month(self):
        self.now = datetime(2026,10,3,12,tzinfo=NY)
        self.auto.enable('paper-broker','data-provider',False,initialize=True)
        result = self.auto.tick(self.worker)
        self.assertTrue(result['startup_check'])
        self.assertEqual([c[0] for c in self.calls], ['calendar','status','refresh'])
        self.assertEqual(self.auto.data['next_run'][:10], '2026-10-05')
        self.assertEqual(self.auto.data['startup']['status'], 'checked')
        self.now = datetime(2026,10,5,9,40,tzinfo=NY)
        self.auto.tick(self.worker); self.next(); self.next()
        self.assertEqual([c[0] for c in self.calls].count('execute'), 1)
        self.assertEqual(self.auto.data['startup']['status'], 'complete')
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'], 'complete')
        self.assertEqual(self.auto.data['next_run'][:10], '2026-11-02')

    def test_midmonth_startup_builds_immediately_once_and_does_not_backfill_monthly_orders(self):
        self.now = datetime(2026,10,2,12,tzinfo=NY)
        self.auto.enable('paper-broker','data-provider',True,initialize=True)
        self.auto.tick(self.worker); self.next(); self.next()
        self.assertEqual([c[0] for c in self.calls], ['calendar','status','refresh','status','prepare','execute'])
        self.assertEqual(self.auto.data['next_run'][:10], '2026-11-02')
        cycle = next(c for c in self.auto.data['cycles'].values() if c.get('kind') == 'initial')
        self.assertEqual(datetime.fromisoformat(cycle['window_start']).hour, 12)
        self.assertEqual(cycle['status'], 'complete')

    def test_repeated_enable_cannot_replace_startup_or_revoke_inflight_generation(self):
        self.auto.enable('paper-broker','data-provider',True,initialize=True)
        before = json.loads(self.auto.path.read_text())
        with self.assertRaises(ControlError):
            self.auto.enable('paper-broker','data-provider',True,initialize=True)
        self.assertEqual(json.loads(self.auto.path.read_text()), before)

    def test_initial_check_survives_restart_without_repeating_collection(self):
        self.auto.enable('paper-broker','data-provider',True,initialize=True)
        self.auto.tick(self.worker)
        self.auto = Automation(self.root,self.registry,clock=lambda:self.now)
        self.next(); self.next()
        self.assertEqual([c[0] for c in self.calls].count('refresh'), 1)
        self.assertEqual([c[0] for c in self.calls].count('execute'), 1)

    def test_initial_envelope_survives_crash_and_only_recovers_original_batch(self):
        self.auto.enable('paper-broker','data-provider',True,initialize=True)
        self.auto.tick(self.worker)
        self.results['execute'] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.next()
        self.auto = Automation(self.root,self.registry,clock=lambda:self.now)
        self.results.pop('execute')
        self.next()
        self.assertEqual(self.calls[-1][0], 'recover')
        self.assertEqual([c[0] for c in self.calls].count('prepare'), 1)
        self.assertEqual(self.auto.data['startup']['status'], 'complete')

    def test_disabling_before_initial_tick_stops_all_workers(self):
        self.auto.enable('paper-broker','data-provider',False,initialize=True)
        self.auto.disable()
        self.assertIsNone(self.auto.tick(self.worker))
        self.assertFalse(self.calls)
        self.assertIsNone(self.auto.data['next_run'])

    def test_daily_cycles_once_per_session(self):
        self.auto.configure({'cadence':'daily'})
        self.enable(); self.auto.tick(self.worker); self.next(); self.next()
        self.now = datetime(2026,10,2,9,40,tzinfo=NY)
        self.auto.tick(self.worker); self.next(); self.next()
        self.assertEqual([c[0] for c in self.calls].count('execute'), 2)
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10-01']['status'], 'complete')
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10-02']['status'], 'complete')

    def test_weekly_cycles_use_separate_keys_and_skip_completed_week(self):
        self.auto.configure({'cadence':'weekly'})
        self.now = datetime(2026,10,5,9,40,tzinfo=NY)
        self.enable(); self.auto.tick(self.worker); self.next(); self.next()
        self.now = datetime(2026,10,12,9,40,tzinfo=NY)
        self.results['calendar'] = dict(status='ready',message='calendar',sessions=['2026-10-05','2026-10-12','2026-10-19','2026-11-02'])
        self.auto.tick(self.worker); self.next(); self.next()
        self.assertEqual([c[0] for c in self.calls].count('execute'), 2)
        self.assertEqual(self.auto.data['cycles']['paper-broker:week-2026-10-05']['status'], 'complete')
        self.assertEqual(self.auto.data['cycles']['paper-broker:week-2026-10-12']['status'], 'complete')
    def test_daily_refresh_then_one_monthly_cycle(self):
        self.enable();self.auto.tick(self.worker);self.next();self.next()
        self.assertEqual([c[0] for c in self.calls],['calendar','refresh','status','prepare','execute'])
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'],'complete')
        self.assertTrue(self.calls[-1][3]['automation'])
        self.assertEqual(self.auto.data['next_run'][:10],'2026-11-02')
    def test_restart_uses_original_envelope_and_recovery_not_new_execute(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.next()
        self.auto=Automation(self.root,self.registry,clock=lambda:self.now)
        self.results.pop('execute')
        self.next()
        self.assertEqual(self.calls[-1][0],'recover')
        self.assertEqual(self.calls[-1][2],'frozen-token')
        self.assertEqual([c[0] for c in self.calls].count('prepare'),1)
    def test_pending_poll_never_creates_another_batch(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='pending')
        self.results['recover']=dict(status='paused',message='still pending',recovery_state='waiting')
        self.next();self.next();self.next()
        self.assertEqual([c[0] for c in self.calls].count('execute'),1)
        self.assertEqual([c[0] for c in self.calls].count('prepare'),1)
        self.assertEqual([c[0] for c in self.calls].count('recover'),2)
    def test_unknown_reconciliation_latches_pause(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='unknown')
        self.next()
        self.results['recover']=dict(status='paused',message='unknown order',recovery_state='blocked')
        self.next()
        self.assertFalse(self.auto.data['enabled'])
        with self.assertRaises(ControlError): self.enable()
    def test_missed_window_does_not_catch_up_orders(self):
        self.now=datetime(2026,10,2,12,tzinfo=NY)
        self.enable();self.auto.tick(self.worker);self.next()
        self.assertNotIn('execute',[c[0] for c in self.calls])
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'],'missed')
    def test_weekend_has_calendar_only(self):
        self.now=datetime(2026,10,3,12,tzinfo=NY)
        self.enable();self.auto.tick(self.worker)
        self.assertEqual([c[0] for c in self.calls],['calendar'])
    def test_data_failures_are_bounded_and_never_place_orders(self):
        self.enable()
        self.results['refresh']=dict(status='blocked',message='provider offline')
        for _ in range(4):
            self.auto.tick(self.worker);self.now+=timedelta(minutes=16)
        self.assertEqual([c[0] for c in self.calls].count('refresh'),3)
        self.assertFalse(self.auto.data['enabled'])
        self.assertNotIn('prepare',[c[0] for c in self.calls])
    def test_binding_change_pauses_before_any_worker(self):
        self.enable();self.registry.context_value='changed'
        self.auto.tick(self.worker)
        self.assertFalse(self.auto.data['enabled'])
        self.assertEqual(self.calls,[])
    def test_code_change_pauses_before_any_worker(self):
        self.enable();(self.root/'strategies').mkdir()
        (self.root/'strategies/new.py').write_text('x=1')
        self.auto.tick(self.worker)
        self.assertFalse(self.auto.data['enabled'])
        self.assertEqual(self.calls,[])
    def test_disabled_guard_rejects_even_with_original_permit(self):
        self.enable()
        permit=dict(generation=self.auto.data['generation'],context='context-one',
                    window_start=(self.now-timedelta(minutes=1)).isoformat(),
                    window_end=(self.now+timedelta(minutes=1)).isoformat())
        with patch('app.module_registry.ModuleRegistry',return_value=self.registry):
            submission_guard(self.root,permit,clock=lambda:self.now)
            self.auto.disable()
            with self.assertRaises(ControlError):submission_guard(self.root,permit,clock=lambda:self.now)
    def test_no_order_month_completes_without_execute(self):
        self.enable();self.auto.tick(self.worker)
        self.results['prepare']=dict(status='complete',message='no change',no_orders=True)
        self.next()
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'],'complete')
        self.assertNotIn('execute',[c[0] for c in self.calls])
    def test_corrupt_state_is_never_overwritten(self):
        self.auto.path.parent.mkdir(parents=True)
        self.auto.path.write_text('broken')
        broken=Automation(self.root,self.registry)
        self.assertFalse(broken.data['enabled'])
        with self.assertRaises(ControlError):broken.configure({})
        self.assertEqual(self.auto.path.read_text(),'broken')

    def test_prior_month_pending_blocks_new_batch(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='pending')
        self.next()
        self.now=datetime(2026,11,2,9,40,tzinfo=NY)
        self.results['reconcile']=dict(status='paused',message='partial',recovery_state='waiting')
        self.auto.tick(self.worker)
        self.assertEqual([c[0] for c in self.calls].count('execute'),1)
        self.assertEqual(self.calls[-1][0],'reconcile')
        self.assertFalse(self.auto.data['enabled'])

    def test_late_fill_closes_cycle_without_window_catchup(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='pending')
        self.next();self.now=datetime(2026,10,1,11,tzinfo=NY)
        self.auto.tick(self.worker)
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'],'closed')
        self.assertEqual(self.calls[-1][0],'reconcile')
        self.assertTrue(self.auto.data['enabled'])
        self.assertEqual([c[0] for c in self.calls].count('execute'),1)

    def test_manual_readonly_completion_resolves_without_rearming(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='unknown')
        self.next();self.auto.disable()
        self.auto.reconciled('other-broker',dict(status='complete',recovery_state='complete'))
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'],'pending')
        self.auto.reconciled('paper-broker',dict(status='ready',recovery_state='ready'))
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'],'pending')
        self.auto.reconciled('paper-broker',dict(status='complete',recovery_state='complete'))
        self.assertFalse(self.auto.data['enabled'])
        self.enable();self.next()
        self.assertEqual([c[0] for c in self.calls].count('execute'),1)

    def test_manual_reconcile_refreshes_paused_message_without_closing_remainder_or_enabling(self):
        self.enable(); self.auto.tick(self.worker)
        self.results['execute'] = dict(status='paused', message='unknown')
        self.next(); self.auto.pause('Previous failure')
        before = dict(self.auto.data['last_run'])
        result = dict(status='ready', recovery_state='ready', details={'orders': [
            {'status': 'filled'}, {'status': 'planned'}, {'status': 'planned'}]})
        self.auto.reconciled('other-broker', result)
        self.assertEqual(self.auto.data['message'], 'Previous failure')
        self.auto.reconciled('paper-broker', result)
        self.assertIn('2 笔未提交计划', self.auto.data['message'])
        self.assertFalse(self.auto.data['enabled'])
        self.assertEqual(self.auto.data['cycles']['paper-broker:2026-10']['status'], 'pending')
        self.assertEqual(self.auto.data['last_run'], before)
        events = len(self.auto.data['events'])
        self.auto.reconciled('paper-broker', result)
        self.assertEqual(len(self.auto.data['events']), events)
        self.assertEqual([c[0] for c in self.calls].count('execute'), 1)

    def test_explicit_resume_only_within_original_unchanged_window(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='pending')
        self.next();self.auto.disable()
        with self.assertRaises(ControlError): self.auto.configure({'trade_time':'10:00'})
        self.enable();self.next()
        self.assertEqual(self.calls[-1][0],'recover')
        self.assertEqual([c[0] for c in self.calls].count('execute'),1)

    def test_resume_refuses_expired_window_and_changed_code(self):
        self.enable();self.auto.tick(self.worker)
        self.results['execute']=dict(status='paused',message='pending')
        self.next();self.auto.disable()
        original=self.now
        self.now=datetime(2026,10,1,11,tzinfo=NY)
        with self.assertRaises(ControlError): self.enable()
        self.now=original
        (self.root/'strategies').mkdir()
        (self.root/'strategies/change.py').write_text('x=1')
        with self.assertRaises(ControlError): self.enable()

    def test_disable_during_inflight_execution_revokes_next_submission_durably(self):
        self.enable();self.auto.tick(self.worker)
        self.now+=timedelta(seconds=31)
        entered,release=threading.Event(),threading.Event()
        transmitted=[];results=[];errors=[];permits=[]
        def worker(root,action,snapshot=None,token=None,progress=None,**kwargs):
            if action!='execute':
                return self.worker(root,action,snapshot,token,progress,**kwargs)
            permits.append(kwargs['automation'])
            entered.set()
            if not release.wait(3):raise AssertionError('test worker did not resume')
            try:
                submission_guard(root,kwargs['automation'],clock=lambda:self.now)
            except ControlError:
                return dict(status='paused',message='revoked before next submission')
            transmitted.append('new order')
            return dict(status='complete',message='unexpected submission')
        def run():
            try:results.append(self.auto.tick(worker))
            except BaseException as error:errors.append(error)
        with patch('app.module_registry.ModuleRegistry',return_value=self.registry):
            thread=threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(3),'execution worker never started')
                generation=self.auto.data['generation']
                self.auto.disable()
                stopped=json.loads(self.auto.path.read_text(encoding='utf-8'))
                self.assertFalse(stopped['enabled'])
                self.assertNotEqual(stopped['generation'],generation)
            finally:
                release.set();thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(transmitted,[])
            self.assertEqual(results[0]['status'],'paused')
            final=json.loads(self.auto.path.read_text(encoding='utf-8'))
            self.assertFalse(final['enabled'])
            self.assertEqual(final['generation'],stopped['generation'])
            with self.assertRaises(ControlError):
                submission_guard(self.root,permits[0],clock=lambda:self.now)
        restarted=Automation(self.root,self.registry,clock=lambda:self.now)
        self.assertFalse(restarted.due())
        self.assertIsNone(restarted.tick(self.worker))

    def test_disable_during_inflight_refresh_cannot_start_later_trades(self):
        self.enable()
        entered,release=threading.Event(),threading.Event()
        errors=[]
        def worker(root,action,*args,**kwargs):
            if action=='refresh':
                entered.set()
                if not release.wait(3):raise AssertionError('test refresh did not resume')
            return self.worker(root,action,*args,**kwargs)
        def run():
            try:self.auto.tick(worker)
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(3),'refresh never started')
            self.auto.disable()
            generation=self.auto.data['generation']
        finally:
            release.set();thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertFalse(self.auto.data['enabled'])
        self.assertFalse(json.loads(self.auto.path.read_text(encoding='utf-8'))['enabled'])
        self.assertEqual(self.auto.data['generation'],generation)
        self.now+=timedelta(minutes=5)
        self.assertIsNone(self.auto.tick(self.worker))
        self.assertNotIn('prepare',[c[0] for c in self.calls])
        self.assertNotIn('execute',[c[0] for c in self.calls])


if __name__=='__main__':unittest.main()
