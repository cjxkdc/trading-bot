"""Portable account workflows: test doubles only, never a broker connection."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from app.control_contracts import SnapshotInput, ControlError
from app.execution_preparation import prepare, execute_prepared
from framework.contracts import OrderState
from framework.journal import Journal
from framework.market import ExecutionContext, InstrumentRules
from framework.order_capabilities import OrderRequirements
from framework.performance import PerformanceStore
from framework.portfolio import StockSnapshot, StockTarget, PortfolioEngine
from framework.runtime import PortfolioRunner
from framework.trading import Account, Quote, ExecutionPolicy, Fees, check_order


class MarketWorkflowTests(unittest.TestCase):
    def workflow(self, market, mode, cash, bid, ask, weight):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root=Path(tmp.name); (root/'runs').mkdir()
        now=datetime.now(timezone.utc)
        from zoneinfo import ZoneInfo
        day=now.astimezone(ZoneInfo(market.timezone)).date()
        stock=StockSnapshot('asset:1','00700' if market.market=='HK' else 'BTC/USD',day,day-timedelta(days=1),'',0.,(1.,2.),(10.,10.))
        account=[Account('bound-'+mode,market.currency,D(cash),(),now)]
        raw={'test_signal':True}; path=root/'runs/signal.json';path.write_text(json.dumps(raw))
        req=OrderRequirements(market.market,market.asset_class,market.currency,'limit','fractional' if market.instrument_rules.quantity_step<1 else 'whole','day')
        loaded=SnapshotInput('portable_test_v1',True,raw,(stock,),{'candidate_count':1},{stock.asset_id:{'ticker':stock.ticker,'industry':''}},order_requirements=req)
        source=SimpleNamespace(execution_signal=lambda raw,clock:((stock,),[]),load=lambda raw:loaded)
        strategy=SimpleNamespace(select=lambda stocks:(StockTarget(stocks[0].asset_id,weight,1.),))
        runner=PortfolioRunner(PortfolioEngine(strategy,max_gross=.95,max_weight=.9),{'type':'fixture'},loaded.schema,
            ExecutionPolicy(max_gross=D('.95'),max_weight=D('.9')),Fees(per_order=D(0)))
        sent=[]
        def quotes(mapping,max_age):
            stamp=datetime.now(timezone.utc)
            return {s:Quote(s,D(bid),D(ask),stamp,rules=market.instrument_rules,currency=market.currency) for s in mapping},stock.price_as_of,stamp
        control=SimpleNamespace(mode=mode,execution_context=lambda:market.record(),observe_account=lambda:replace(account[0],as_of=datetime.now(timezone.utc)),
            observe_quotes=quotes,entry={'control':{'order_capabilities':{k:[v] for k,v in req.record().items()}}})
        class Broker:
            live_enabled=mode=='live'
            def account(self): return control.observe_account()
            def execution_time(self): return datetime.now(timezone.utc)
            def open_orders(self): return ()
            def submit(self,order):
                live_quotes,_,_=quotes({order.symbol:'unused',**{s:'unused' for s,q in account[0].positions}},120)
                account[0]=check_order(order,self.account(),live_quotes,self.execution_time(),runner.policy,runner.fees,execution_context=market)
                sent.append(order)
                return OrderState(order.client_id,'test-'+order.client_id,'filled',order.quantity,order.limit_price)
        Broker.mode=mode
        @contextmanager
        def session(*args):
            journal=Journal(root/'journal.sqlite')
            try: yield Broker(),journal
            finally: journal.close()
        control.session=session
        preview=prepare(root=root,source=source,loaded=loaded,runner=runner,control=control,path=path,context='binding',clock=now,progress=lambda _:None)
        frozen=json.loads(Path(preview['snapshot_path']).read_text())
        self.assertEqual(frozen['execution_context'],market.record())
        self.assertEqual(frozen['environment'],mode)
        result=execute_prepared(root=root,raw=frozen,context='binding',token=preview['preview_token'],source=source,runner=runner,control=control,cfg={},clock=now)
        self.assertEqual(result['status'],'complete')
        self.assertTrue(sent)
        self.assertTrue(all(market.instrument_rules.valid_quantity(o.quantity) for o in sent))
        with self.assertRaises(ControlError):
            execute_prepared(root=root,raw=frozen,context='binding',token=preview['preview_token'],source=source,runner=runner,
                control=SimpleNamespace(**dict(vars(control),mode='broker_paper' if mode=='live' else 'live')),cfg={},clock=now)
        return sent,preview

    def test_hkd_lot_account_uses_hong_kong_context_through_fill(self):
        context=ExecutionContext('HK','Asia/Hong_Kong','HKD','equity',InstrumentRules(D(100),D(100),D('.05'),D(1)))
        orders,_=self.workflow(context,'broker_paper','100000','100','100.05',.5)
        self.assertEqual(sum(o.quantity for o in orders),D(400))

    def test_fractional_live_contract_interface_uses_multiplier_without_network(self):
        context=ExecutionContext('GLOBAL','UTC','USDT','spot',InstrumentRules(D('.001'),D('.001'),D('.01'),D(2)))
        orders,preview=self.workflow(context,'live','10000','999','1000',.5)
        self.assertEqual(sum(o.quantity for o in orders),D('2.5'))
        self.assertEqual(sum(D(o['notional']) for o in preview['orders']),D(5000))


class MultiCurrencyPerformanceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=PerformanceStore(Path(tmp.name)/'performance.sqlite')

    def observation(self,stamp='2026-10-02T15:59:00Z',**changes):
        return dict(dict(environment='REAL',account_ref='hk-account',currency='HKD',timezone='Asia/Hong_Kong',
            as_of=stamp,equity=1000,positions=[]),**changes)

    def test_real_hkd_daily_candles_follow_hong_kong_midnight(self):
        self.store.record('hk',self.observation())
        self.store.record('hk',self.observation('2026-10-02T16:01:00Z',equity=1100))
        result=self.store.candles('hk','1d')
        self.assertEqual((result['currency'],result['timezone']),('HKD','Asia/Hong_Kong'))
        self.assertEqual(len(result['bars']),2)
        self.assertEqual(result['bars'][1]['start'],'2026-10-02T16:00:00+00:00')
        self.assertEqual(self.store.report('hk')['currency'],'HKD')

    def test_same_scope_cannot_mix_currency_environment_or_timezone(self):
        self.store.record('hk',self.observation())
        for changes in ({'currency':'USD'},{'environment':'SIMULATE'},{'timezone':'UTC'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.store.record('hk',self.observation('2026-10-02T16:01:00Z',**changes))

    def test_non_us_account_requires_timezone_and_can_observe_short_position(self):
        raw=self.observation();raw.pop('timezone')
        with self.assertRaises(ValueError):self.store.record('hk',raw)
        result=self.store.record('hk',self.observation(positions=[{'symbol':'00700','quantity':-100}]))
        self.assertEqual(result['positions'][0]['quantity'],-100)

    def test_foreign_position_valuation_requires_explicit_conversion(self):
        with self.assertRaisesRegex(ValueError, 'FX conversion'):
            self.store.record('hk', self.observation(positions=[
                {'symbol':'SPY', 'quantity':1, 'currency':'USD', 'market_value':600}]))

    def test_non_us_benchmark_needs_explicit_matching_currency(self):
        for currency in (None, 'USD', 'HKD'):
            scope = 'bench-' + str(currency)
            for day, price in ((1, 100), (2, 110)):
                benchmark = dict(symbol='TEST', price=price, session_day=f'2026-10-0{day}',
                                 completed_at=f'2026-10-0{day}T08:00:00Z')
                if currency: benchmark['currency'] = currency
                self.store.record(scope, self.observation(f'2026-10-0{day}T09:00:00Z', benchmark=benchmark))
            value = self.store.report(scope)['metrics']['benchmark_return']
            if currency == 'HKD': self.assertAlmostEqual(value, .1)
            else: self.assertIsNone(value)
