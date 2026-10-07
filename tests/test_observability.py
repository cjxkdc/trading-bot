"""Accounting and notifications are read-only, durable, and never enable execution."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, date, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from app.control_contracts import ControlError
from app.monitoring import Monitoring
from app.notifications import NotificationSettings, Telegram
from app.workbench import PortalService
from brokers.opend_observation import financial_observation
from framework.events import EventStore
from framework.performance import PerformanceStore, number
from framework.scheduling import schedule_config, initial_window, selection_period
from tools.build_quantconnect import build

ROOT=Path(__file__).resolve().parents[1]


def observation(equity, at, **extra):
    return dict(environment='SIMULATE',account_ref='paper-account',currency='USD',as_of=at,
                equity=equity,cash_total=equity,cash_available=500,market_value=0,positions=[],**extra)


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=PerformanceStore(Path(self.temp.name)/'performance.sqlite')

    def record(self, equity, day, **extra):
        return self.store.record('a', observation(equity,f'2025-01-{day:02d}T21:00:00+00:00',**extra))

    def test_nav_never_uses_buying_power_and_unknown_is_not_zero(self):
        raw=observation(1000,'2025-01-02T21:00:00+00:00')
        self.store.record('a',raw)
        report=self.store.report('a')
        self.assertEqual(report['latest']['equity'],1000)
        self.assertEqual(report['latest']['cash_available'],500)
        self.assertIsNone(report['latest']['fees'])
        self.assertIsNone(report['metrics']['return_rate'])
        self.assertNotIn('fees',raw)

    def test_deposit_does_not_become_profit_and_dividends_are_not_subtracted(self):
        self.record(1000,2)
        last=self.record(1600,3)
        self.store.cash_event('a',dict(as_of='2025-01-03T21:00:00Z',kind='deposit',amount=500,note=''))
        self.store.cash_event('a',dict(as_of='2025-01-03T20:00:00Z',kind='dividend',amount=10,note='already in NAV'))
        self.store.review_cash('a',last['as_of'])
        report=self.store.report('a')
        self.assertAlmostEqual(report['metrics']['return_rate'],.1)
        self.assertAlmostEqual(report['metrics']['asset_change'],.6)

    def test_weighted_flow_timing_and_drawdown(self):
        self.record(1000,2)
        self.record(1650,3)
        self.store.cash_event('a',dict(as_of='2025-01-03T09:00:00Z',kind='deposit',amount=500,note='halfway'))
        last=self.record(1485,6)
        self.store.review_cash('a',last['as_of'])
        report=self.store.report('a')
        self.assertAlmostEqual(report['metrics']['return_rate'],1.12*.9-1)
        self.assertAlmostEqual(report['metrics']['max_drawdown'],.1)

    def test_account_and_currency_mismatch_fail_without_replacing_history(self):
        self.record(1000,2)
        for key,value in [('account_ref','another-account'),('environment','REAL'),('currency','HKD'),('equity',float('nan'))]:
            raw=observation(2000,'2025-01-03T21:00:00Z');raw[key]=value
            with self.assertRaises(ValueError):self.store.record('a',raw)
        self.assertEqual(self.store.report('a')['latest']['equity'],1000)
        with self.assertRaises(ValueError):number('1e9999')

    def test_duplicate_observation_is_idempotent_but_conflict_fails(self):
        self.record(1000,2);self.record(1000,2)
        with self.assertRaises(ValueError):self.record(1200,2)
        self.assertEqual(len(self.store.report('a')['curve']),1)

    def test_new_observation_invalidates_return_review_and_reset_starts_new_period(self):
        self.record(1000,2);last=self.record(1200,3)
        self.store.review_cash('a',last['as_of'])
        self.record(5000,6)
        self.assertIsNone(self.store.report('a')['metrics']['return_rate'])
        self.store.cash_event('a',dict(as_of='2025-01-06T21:00:00Z',kind='reset',amount=0,note='paper reset'))
        last=self.record(5100,7)
        self.store.review_cash('a',last['as_of'])
        self.assertAlmostEqual(self.store.report('a')['metrics']['return_rate'],.02)

    def test_flow_correction_and_account_bound_deletion(self):
        self.record(1000,2);last=self.record(1100,3)
        ident=self.store.cash_event('a',dict(as_of=last['as_of'],kind='deposit',amount=50,note='mistake'))
        with self.assertRaises(ValueError):self.store.remove_cash_event('b',ident)
        self.store.review_cash('a',last['as_of'])
        self.store.remove_cash_event('a',ident)
        self.assertIsNone(self.store.report('a')['metrics']['return_rate'])
        with self.assertRaises(ValueError):
            self.store.cash_event('a',dict(as_of='2025-01-08T21:00:00Z',kind='reset',amount=0,note='unobserved'))

    def test_late_benchmark_close_aligns_to_matching_account_dates(self):
        self.record(1000,2)
        self.record(1100,3,benchmark=dict(symbol='QQQ',price=400,session_day='2025-01-02'))
        last=self.record(1200,6,benchmark=dict(symbol='QQQ',price=420,session_day='2025-01-03'))
        self.store.review_cash('a',last['as_of'])
        metrics=self.store.report('a')['metrics']
        self.assertAlmostEqual(metrics['benchmark_return'],.05)
        self.assertAlmostEqual(metrics['benchmark_period']['account_return'],.1)
        self.assertEqual(metrics['benchmark_period']['end'],'2025-01-03')

    def test_preview_does_not_change_executed_target_weights(self):
        self.record(1000,2)
        self.store.cycle('a',dict(decision_id='done',targets=[dict(ticker='A',weight=.5)],orders=[],status='complete'))
        self.store.cycle('a',dict(decision_id='draft',targets=[dict(ticker='B',weight=.9)],orders=[],status='previewed'))
        report=self.store.report('a')
        self.assertEqual(report['positions'][0]['symbol'],'A')
        self.assertEqual(report['positions'][0]['target_weight'],.5)


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.events=EventStore(self.root/'events.sqlite')

    def test_durable_deduplication_and_recovery_notifications(self):
        self.events.transition('connection',True,'healthy',notify=True)
        self.assertEqual(self.events.recent(),[])
        self.events.transition('connection',False,'failed',notify=True)
        self.events=EventStore(self.root/'events.sqlite')
        self.events.transition('connection',False,'still failed',notify=True)
        self.events.transition('connection',True,'recovered',notify=True)
        self.assertEqual(len(self.events.recent()),2)

    def test_delivery_backs_off_is_bounded_and_never_logs_transport_secrets(self):
        self.events.emit('a','safe',notify=True)
        send=Mock(side_effect=RuntimeError('secret-token'))
        self.assertTrue(self.events.deliver_one(send,now=0))
        self.assertFalse(self.events.deliver_one(send,now=1))
        self.assertTrue(self.events.deliver_one(send,now=60))
        self.assertTrue(self.events.deliver_one(send,now=180))
        self.assertFalse(self.events.deliver_one(send,now=1000))
        self.assertEqual(self.events.recent()[0]['delivery'],'failed')
        self.assertNotIn('secret-token',str(self.events.recent()))

    def test_success_is_not_resent_after_restart_and_disabled_events_not_queued(self):
        self.events.emit('a','local')
        self.events.emit('b','notify',notify=True)
        send=Mock();self.events.deliver_one(send)
        restarted=EventStore(self.root/'events.sqlite')
        restarted.emit('b','notify',notify=True)
        self.assertFalse(restarted.deliver_one(send))
        send.assert_called_once()

    def test_secret_settings_and_explicit_outbound_configuration(self):
        cfg=NotificationSettings(self.root)
        self.assertEqual(cfg.config['channel'],'none')
        token='123456:'+'x'*30
        cfg.save(dict(channel='telegram',telegram_token=token,telegram_chat_id='1234'))
        self.assertNotIn(token,json.dumps(cfg.public()))
        self.assertNotIn('1234',json.dumps(cfg.public()))
        cfg.save(dict(telegram_token='',telegram_chat_id=''))
        self.assertEqual(NotificationSettings(self.root).config['telegram_token'],token)
        for url in ['http://hc-ping.com/a','https://127.0.0.1/','https://hc-ping.com@evil.example/id']:
            with self.assertRaises(ControlError):cfg.save(dict(heartbeat_url=url))
        with patch('app.notifications.request_json',return_value=b'{"ok":false}'):
            with self.assertRaises(ValueError):Telegram.send(cfg.config,dict(id='safe',at='today',severity='info',message='test'))


class BrokerObservationTests(unittest.TestCase):
    def test_bound_paper_nav_with_pending_order_does_not_use_buying_power(self):
        from brokers.opend_broker import _identity
        sdk=NS(TrdEnv=NS(SIMULATE='SIMULATE'),Currency=NS(USD='USD'),TrdMarket=NS(NONE='NONE'),PositionSide=NS(LONG='LONG'))
        profile=dict(provider='moomoo',environment='SIMULATE',user_id='1',account_id='2')
        @contextmanager
        def session():yield sdk,object(),profile,None
        connector=NS(mode='broker_paper',environment='SIMULATE',_session=session)
        funds=[dict(currency='USD',total_assets=1000,us_cash=900,usd_net_cash_power=400,market_val=100)]
        positions=[dict(code='US.AAPL',currency='USD',position_side='LONG',qty=1,market_val=100,
            stock_name='Apple',nominal_price=100,cost_price_valid=True,cost_price=90,pl_val=10,
            pl_val_valid=True,pl_ratio_valid=True,pl_ratio=11.11,unrealized_pl=999)]
        def call(client,method,**kwargs):
            self.assertEqual(kwargs['trd_env'],'SIMULATE');self.assertEqual(kwargs['acc_id'],2)
            return funds if method=='accinfo_query' else positions
        with patch('brokers.opend_observation.call',side_effect=call), patch('brokers.opend_observation.records',side_effect=lambda v:v), patch('brokers.opend_observation._orders',return_value=[{'order_status':'SUBMITTED'}]):
            result=financial_observation(connector)
            self.assertEqual(result['equity'],1000);self.assertEqual(result['cash_available'],400)
            self.assertEqual(result['open_order_count'],1)
            self.assertEqual(result['account_ref'],_identity(profile))
            position=result['positions'][0]
            self.assertEqual(position['position_pnl'],10)
            self.assertEqual(position['name'],'Apple')
            self.assertEqual(position['last_price'],100)
            self.assertAlmostEqual(position['pnl_rate'],.1111)
            self.assertIsNone(position['unrealized_pnl'])
            for flag in (False,None):
                positions[0].update(pl_val_valid=flag,pl_ratio_valid=flag,cost_price_valid=flag)
                invalid=financial_observation(connector)['positions'][0]
                for key in ('position_pnl','pnl_rate','cost_price'):
                    self.assertIsNone(invalid[key])
            funds[0]['currency']='HKD'
            with self.assertRaises(ControlError):financial_observation(connector)
        connector.environment='REAL'
        with self.assertRaises(ControlError):financial_observation(connector)

    def test_missing_display_currency_requires_us_only_paper_profile(self):
        sdk=NS(TrdEnv=NS(SIMULATE='SIMULATE'),Currency=NS(USD='USD'),TrdMarket=NS(NONE='NONE'),PositionSide=NS(LONG='LONG'))
        profile=dict(provider='moomoo',environment='SIMULATE',user_id='1',account_id='2',
                     sim_acc_type='STOCK_AND_OPTION',trdmarket_auth=('US',))
        @contextmanager
        def session():yield sdk,object(),profile,None
        connector=NS(mode='broker_paper',environment='SIMULATE',_session=session)
        funds=[dict(currency='N/A',total_assets=1000,us_cash=900,frozen_cash=25,market_val=100,power=2000)]
        def call(client,method,**kwargs):
            self.assertEqual(kwargs['trd_env'],'SIMULATE');self.assertEqual(kwargs['acc_id'],2)
            return funds if method=='accinfo_query' else []
        with patch('brokers.opend_observation.call',side_effect=call), patch('brokers.opend_observation.records',side_effect=lambda v:v), patch('brokers.opend_observation._orders',return_value=[]):
            for account_type in ('STOCK','STOCK_AND_OPTION'):
                profile['sim_acc_type']=account_type
                for missing in ('N/A',None,''):
                    funds[0]['currency']=missing
                    result=financial_observation(connector)
                    self.assertEqual(result['currency'],'USD');self.assertEqual(result['equity'],1000)
            self.assertEqual(result['cash_available'],875)
            for currency,markets,account_type in [('HKD',('US',),'STOCK_AND_OPTION'),
                    ('N/A',('US','HK'),'STOCK_AND_OPTION'),('N/A',(),'STOCK'),('N/A',('US',),'FUTURES')]:
                funds[0]['currency']=currency;profile.update(trdmarket_auth=markets,sim_acc_type=account_type)
                with self.assertRaises(ControlError):financial_observation(connector)


class MonitoringServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for folder in ('configs','strategies','framework'):
            shutil.copytree(ROOT/folder,self.root/folder,ignore=shutil.ignore_patterns('__pycache__'))
        for folder in ('brokers','data'):
            shutil.copytree(ROOT/folder/'plugins',self.root/folder/'plugins')
        self.service=PortalService(self.root,worker=Mock())

    def test_monitor_still_runs_while_automation_is_off_and_does_not_enable_it(self):
        service=self.service
        self.assertFalse(service.automation.data['enabled'])
        with patch.object(service.registry,'descriptor',return_value={'capabilities':['observe']}), patch.object(service,'start') as start, patch.object(service.monitor,'pump'):
            service.monitoring_poll()
            start.assert_called_once_with('observe')
            self.assertFalse(service.automation.data['enabled'])
            service.active={'action':'execute'};service.monitor.next_observation=0
            service.monitoring_poll();start.assert_called_once()

    def test_observer_failure_preserves_last_valid_equity_and_emits_once(self):
        monitor=self.service.monitor
        raw=observation(1000,'2025-01-02T21:00:00Z')
        with patch.object(monitor,'scope',return_value='paper-account'):
            monitor.observed('paper',dict(status='complete',observation=raw),'paper-account')
            for _ in range(2):monitor.observed('paper',dict(status='blocked'),'paper-account')
            self.assertEqual(monitor.performance.report('paper-account')['latest']['equity'],1000)
            self.assertEqual(len(monitor.events.recent()),1)
            with self.assertRaises(ControlError):monitor.observed('paper',dict(status='complete',observation=raw),'wrong')

    def test_performance_actions_reject_stale_account_view(self):
        service=self.service
        with patch.object(service.monitor,'scope',return_value='paper-account'):
            service.monitor.performance.record('paper-account',observation(1000,'2025-01-02T21:00:00Z'))
            report=service.performance()
            service.monitor.performance.record('paper-account',observation(1100,'2025-01-03T21:00:00Z'))
            with self.assertRaises(ControlError):
                service.performance_review(dict(through=report['latest']['as_of'],view_token=report['view_token']))


class SharedScheduleTests(unittest.TestCase):
    def test_first_entry_and_friday_holiday_rollover(self):
        sessions=[date(2025,7,3),date(2025,7,7),date(2025,7,8),date(2025,7,11)]
        cfg=schedule_config(dict(cadence='weekly',weekday=4,initial_entry='scheduled'))
        self.assertEqual(selection_period(date(2025,7,7),sessions,cfg),'week-2025-06-30')
        self.assertIsNone(selection_period(date(2025,7,8),sessions,cfg))
        cfg['initial_entry']='immediate'
        self.assertIsNotNone(selection_period(date(2025,7,8),sessions,cfg,first=True))
        cfg['initial_entry']='scheduled'
        start,end=initial_window(datetime(2025,7,8,12,tzinfo=timezone.utc),sessions,cfg)
        self.assertEqual(start.date(),date(2025,7,11))

    def test_qc_export_carries_complete_plan_without_connection_details(self):
        from framework.configuration import load_config
        cfg=load_config(ROOT/'tests/fixtures/portfolio_default.json')
        cfg['schedule']=schedule_config(dict(cadence='weekly',weekday=2,trade_time='10:15',initial_entry='scheduled'))
        code=build(cfg)
        self.assertIn("'trade_time': '10:15'",code)
        self.assertIn("'cadence': 'weekly'",code)
        self.assertIn('def selection_period(',code)
        self.assertNotIn('secrets/',code)


if __name__=='__main__':unittest.main()
