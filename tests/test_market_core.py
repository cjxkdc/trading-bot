"""Market-independent execution tests use in-memory brokers, never external accounts."""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from framework.market import ExecutionContext, InstrumentRules
from framework.contracts import OrderIntent, OrderState
from framework.decisions import Decision, digest
from framework.execution import Executor, ExecutionBlocked
from framework.journal import Journal
from framework.portfolio import StockTarget
from framework.scheduling import (schedule_config, initial_window, period_window,
                                  normalize_sessions, schedule_defaults)
from framework.strategy_book import owner_plan, settle_ownership
from app.strategy_instances import StrategyInstances
from app.control_contracts import ControlError
from framework.trading import Account, Quote, ExecutionPolicy, Fees, check_order, plan_orders

AT = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
HK = ExecutionContext('HK', 'Asia/Hong_Kong', 'HKD', 'equity', InstrumentRules('100','100','.05','1'))
CRYPTO = ExecutionContext('GLOBAL','UTC','USDT','crypto', InstrumentRules('.001','.001','.01','1'))
POLICY = ExecutionPolicy(max_gross=D('.9'),max_weight=D('.9'),max_order_value=D('100000'))
FEES = Fees(D('0'),D('0'))


def decision(context, targets=(('asset', .5),)):
    ts = tuple(StockTarget(s,w,0.) for s,w in targets)
    record = dict(execution_day=AT.astimezone(ZoneInfo(context.timezone)).date().isoformat(),
                  execution_context=context.record(), targets=[asdict(t) for t in ts])
    return Decision(digest(record), ts, record)


class FakeLiveBroker:
    mode = 'live'
    live_enabled = True
    read_only = False
    def __init__(self, context, quotes, cash):
        self.context,self.quotes=context,quotes
        self.current=Account('fake-live-'+context.market,context.currency,D(cash),(),AT)
        self.sent=[];self.orders={};self.lose_ack=False
    def execution_time(self): return AT
    def account(self): return self.current
    def open_orders(self): return ()
    def query(self, ident): return self.orders[ident]
    def submit(self, order):
        self.sent.append(order)
        self.current=check_order(order,self.current,self.quotes,AT,POLICY,FEES,execution_context=self.context)
        self.orders[order.client_id]=OrderState(order.client_id,'venue-'+order.client_id,'filled',order.quantity,order.limit_price)
        if self.lose_ack:
            self.lose_ack=False
            raise ConnectionError('simulated lost acknowledgement')
        return self.orders[order.client_id]


class MarketRulesTests(unittest.TestCase):
    def test_hk_context_roundtrips_and_sizes_board_lots_in_hkd(self):
        context=ExecutionContext.from_record(HK.record())
        quotes={'asset':Quote('asset',D('9.98'),D('10.02'),AT,currency='HKD')}
        account=Account('hk','HKD',D(100000),(),AT)
        orders=plan_orders(decision(context),account,quotes,AT,POLICY,FEES,execution_context=context)
        self.assertEqual(orders[0].quantity,D(4900))
        self.assertEqual(orders[0].limit_price,D('10.05'))
        self.assertEqual(orders[0].quantity % 100,0)
        after=check_order(orders[0],account,quotes,AT,POLICY,FEES,execution_context=context)
        self.assertEqual(after.currency,'HKD')
        self.assertEqual(after.cash,D(50755))

    def test_fractional_crypto_orders_and_fills_are_preserved(self):
        quotes={'asset':Quote('asset',D(2000),D(2000),AT,currency='USDT')}
        account=Account('crypto','USDT',D(1000),(),AT)
        orders=plan_orders(decision(CRYPTO),account,quotes,AT,POLICY,FEES)
        self.assertEqual(orders[0].quantity,D('.25'))
        self.assertEqual(check_order(orders[0],account,quotes,AT,POLICY,FEES,execution_context=CRYPTO).cash,D(500))

    def test_symbol_rules_override_context_rules(self):
        fractional=InstrumentRules('.01','.01','.001','1')
        quotes={'asset':Quote('asset',D('3'),D('3.001'),AT,fractional,'HKD')}
        account=Account('hk','HKD',D(100),(),AT)
        orders=plan_orders(decision(HK),account,quotes,AT,POLICY,FEES)
        self.assertEqual(orders[0].quantity % D('.01'),0)
        self.assertGreater(orders[0].quantity,0)
        self.assertLess(orders[0].quantity,100)

    def test_minimum_order_larger_than_step_does_not_break_binary_sizing(self):
        context=ExecutionContext('TEST','UTC','EUR','equity',InstrumentRules('1','100','.01','1'))
        quotes={'asset':Quote('asset',D(10),D(10),AT,currency='EUR')}
        account=Account('eur','EUR',D(2000),(),AT)
        orders=plan_orders(decision(context),account,quotes,AT,POLICY,FEES)
        self.assertEqual(orders[0].quantity,D(100))
        # A previous partial fill may leave fewer than the new-order minimum.
        small=replace(account,positions=(('asset',D(2)),))
        orders=plan_orders(decision(context),small,quotes,AT,POLICY,FEES)
        self.assertEqual(orders,())

    def test_currency_mismatch_rejected_before_sizing(self):
        quotes={'asset':Quote('asset',D(10),D(10),AT,currency='USD')}
        account=Account('hk','HKD',D(1000),(),AT)
        with self.assertRaisesRegex(ValueError,'Quote currency'):
            plan_orders(decision(HK),account,quotes,AT,POLICY,FEES)
        with self.assertRaisesRegex(ValueError,'Account currency'):
            plan_orders(decision(HK),replace(account,currency='USD'),quotes,AT,POLICY,FEES)

    def test_multiplier_is_in_cash_exposure_and_order_cap(self):
        context=ExecutionContext('TEST','UTC','EUR','fund',InstrumentRules('1','1','.01','10'))
        quote=Quote('asset',D(10),D(10),AT,currency='EUR')
        account=Account('eur','EUR',D(1000),(),AT)
        order=plan_orders(decision(context),account,{'asset':quote},AT,POLICY,FEES)[0]
        self.assertEqual(order.quantity,D(5))
        self.assertEqual(check_order(order,account,{'asset':quote},AT,POLICY,FEES,execution_context=context).cash,D(500))
        with self.assertRaisesRegex(ValueError,'notional'):
            check_order(order,account,{'asset':quote},AT,replace(POLICY,max_order_value=D(100)),FEES,execution_context=context)

    def test_managed_fractional_ownership_never_truncates_positions(self):
        rules={'asset':CRYPTO.instrument_rules}
        owners,net=owner_plan({}, {}, {'a':{'asset':1},'b':{'asset':1}}, {'a':'.3','b':'.4'},D(1000),{'asset':D(2000)},instrument_rules=rules)
        self.assertEqual(net,{'asset':'0.35'})
        final=settle_ownership(owners,{}, {}, {'asset':'.349'}, ['a','b'],instrument_rules=rules)
        self.assertEqual(sum(D(p['asset']) for p in final.values()),D('.349'))
        self.assertEqual(final['a']['asset'],'0.150')


class LiveGenericExecutionTests(unittest.TestCase):
    def test_live_hk_approved_batch_executes_and_recovers_without_resubmission(self):
        quotes={'asset':Quote('asset',D(10),D(10),AT,currency='HKD')}
        broker=FakeLiveBroker(HK,quotes,'10000')
        initial=broker.account(); selected=decision(HK)
        orders=plan_orders(selected,initial,quotes,AT,POLICY,FEES)
        with tempfile.TemporaryDirectory() as folder:
            journal=Journal(Path(folder)/'journal.sqlite')
            self.addCleanup(journal.close)
            runner=Executor(broker,journal,POLICY,FEES)
            broker.lose_ack=True
            with self.assertRaises(ConnectionError):
                runner.run(selected,quotes,AT,approved_orders=orders,expected_account=initial)
            result=runner.run(selected,quotes,AT,approved_orders=orders,expected_account=initial,recover=True)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(len(broker.sent),1)
            self.assertEqual(broker.account().positions,(('asset',D(500)),))
            self.assertEqual(runner.reconcile()['recovery_state'],'complete')
            journal.close()

    def test_live_fractional_fill_is_valid_and_readonly_cannot_trade(self):
        quotes={'asset':Quote('asset',D(2000),D(2000),AT,currency='USDT')}
        broker=FakeLiveBroker(CRYPTO,quotes,'1000')
        with tempfile.TemporaryDirectory() as folder:
            journal=Journal(Path(folder)/'journal.sqlite')
            try:
                runner=Executor(broker,journal,POLICY,FEES)
                runner.run(decision(CRYPTO),quotes,AT)
                self.assertEqual(broker.account().positions,(('asset',D('.25')),))
                broker.read_only=True;broker.live_enabled=False
                readonly=Executor(broker,journal,POLICY,FEES)
                self.assertEqual(readonly.reconcile()['submitted'],0)
                with self.assertRaisesRegex(ExecutionBlocked,'read-only'):
                    readonly.run(decision(CRYPTO),quotes,AT)
            finally: journal.close()

    def test_live_disabled_fails_before_any_submission(self):
        broker=FakeLiveBroker(HK,{},'1000');broker.live_enabled=False
        with self.assertRaisesRegex(ValueError,'explicitly enabled'):
            Executor(broker,None,POLICY,FEES)
        self.assertFalse(broker.sent)


class MarketCalendarTests(unittest.TestCase):
    def hk_session(self):
        return [{'date':'2026-10-08','intervals':[
            {'open':'2026-10-08T09:30:00+08:00','close':'2026-10-08T12:00:00+08:00'},
            {'open':'2026-10-08T13:00:00+08:00','close':'2026-10-08T16:00:00+08:00'}]}]
    def test_hk_lunch_break_and_close_clip_windows(self):
        cfg=schedule_config({'cadence':'daily','timezone':'Asia/Hong_Kong','trade_time':'11:40'})
        start,end,_=period_window(date(2026,10,8),self.hk_session(),cfg)
        self.assertEqual(end.hour,12)
        lunch=datetime(2026,10,8,12,20,tzinfo=ZoneInfo('Asia/Hong_Kong'))
        opening,_=initial_window(lunch,self.hk_session(),cfg)
        self.assertEqual(opening.hour,13)

    def test_24x7_weekend_calendar_and_midnight_schedule(self):
        cfg=schedule_config({'cadence':'daily','timezone':'UTC','prepare_time':'00:00','trade_time':'00:00'})
        days=[{'date':'2026-10-10','intervals':[{'open':'2026-10-10T00:00:00+00:00','close':'2026-10-11T00:00:00+00:00'}]}]
        now=datetime(2026,10,10,12,tzinfo=timezone.utc)
        self.assertEqual(initial_window(now,days,cfg)[0],now)
        self.assertEqual(period_window(date(2026,10,10),days,cfg)[0].hour,0)

    def test_other_market_requires_actual_intervals_not_us_hours(self):
        cfg=schedule_config({'timezone':'Asia/Tokyo'})
        with self.assertRaisesRegex(ValueError,'explicit session intervals'):
            initial_window(AT,[date(2026,10,8)],cfg)

    def test_module_defaults_and_calendar_validation(self):
        self.assertEqual(schedule_defaults(HK)['timezone'],'Asia/Hong_Kong')
        self.assertEqual(schedule_defaults(CRYPTO)['trade_time'],'00:00')
        self.assertEqual(schedule_config({'timezone':'UTC','trade_time':'16:00'})['trade_time'],'16:00')
        bad=self.hk_session();bad[0]['intervals'][1]['open']='2026-10-08T11:00:00+08:00'
        with self.assertRaisesRegex(ValueError,'overlap'):
            normalize_sessions(bad)


class InstanceEnvironmentTests(unittest.TestCase):
    def test_live_instance_can_be_configured_but_needs_account_grant_to_start(self):
        class Registry:
            granted=False
            def descriptor(self,kind,ident):
                return {'enabled':True,'mode':'live','execution_enabled':self.granted,
                        'execution_context':HK.record(),
                        'capabilities':['prepare','execute','reconcile','calendar'] if kind=='broker' else ['collect','execution_signal']}
            def configuration(self,broker): return {}
            def validate_strategy(self,cfg): pass
            def compatible(self,cfg,source): pass
            def source_context(self,source): return 'source'
            def connection_context(self,broker): return 'account'
        with tempfile.TemporaryDirectory() as folder:
            registry=Registry();manager=StrategyInstances(folder,registry,clock=lambda:AT)
            ident=manager.create(dict(name='HK live',strategy={'type':'example'},broker_id='arbitrary-provider',
                        source_id='other-data',budget='.5',schedule={}))['id']
            item=manager.get(ident)
            self.assertFalse(item['enabled'])
            self.assertEqual(item['schedule']['timezone'],'Asia/Hong_Kong')
            with self.assertRaises(ControlError): manager.start(ident)
            self.assertFalse(item['enabled'])
            registry.granted=True
            manager.start(ident)
            self.assertTrue(item['enabled'])
            registry.granted=False
            manager.tick(lambda *a,**k:self.fail('Revoked execution must stop before worker'))
            self.assertFalse(item['enabled'])


if __name__ == '__main__': unittest.main()
