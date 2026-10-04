"""Whole paper workflow with real sizing/journalling and no network or real orders."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

from app.assembly import build_portfolio_runner
from app.control_contracts import ControlError, SnapshotInput
from app.paper_preparation import prepare, execute_prepared, mapping_for
from brokers.opend_observation import quote_observation
from framework.contracts import OrderState
from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS
from framework.decisions import digest
from framework.execution import ExecutionBlocked, Executor
from framework.journal import Journal
from framework.portfolio import EquitySnapshot, PortfolioEngine
from framework.trading import Account, ExecutionPolicy, Fees, Quote, check_order, plan_orders
from strategies.mom8 import MOM8


class TestPreparation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=datetime.now(timezone.utc)
        day=self.now.astimezone(ZoneInfo('America/New_York')).date()
        self.previous=day-timedelta(days=1)
        self.stocks=tuple(EquitySnapshot('FIGI:'+str(i),'STOCK'+str(i),day,self.previous,str(i),float('nan'),
            tuple(100.+j for j in range(253)),(),3000000000.,352.,(2000000.,)*20) for i in range(8))
        self.account=Account('test-paper','USD',D(1000000),(),self.now)
        self.raw={'fixture':'current-observation'}
        self.path=self.root/'runs/signal.json';self.path.parent.mkdir();self.path.write_text(json.dumps(self.raw))
        self.loaded=SnapshotInput('us_equity_momentum_daily_v1',True,self.raw,self.stocks,
            {'candidate_count':8}, {s.asset_id:{'ticker':s.ticker,'industry':s.industry} for s in self.stocks}, order_requirements=US_EQUITY_LIMIT_ORDERS)
        self.source=SimpleNamespace(paper_signal=lambda raw,clock:(self.stocks,['Current observations, not historical PIT.']),load=lambda raw:self.loaded)
        cfg={'strategy':{'type':'mom8'},'initial_cash':'1000000',
             'limits':{'max_gross':'.95','max_weight':'.19','max_order_value':'100000','max_quote_age_seconds':120},
             'fees':{'per_order':'1','bps':'0'}}
        self.cfg=cfg;self.runner=build_portfolio_runner(cfg)
        def quotes(mapping,max_age):
            stamp=datetime.now(timezone.utc)
            return {s:Quote(s,D('99.99'),D('100'),stamp) for s in mapping},self.previous,stamp
        self.control=SimpleNamespace(observe_account=lambda:self.account,observe_quotes=quotes,
            entry={'control':{'order_capabilities':{k:[v] for k,v in US_EQUITY_LIMIT_ORDERS.record().items()}}})

    def preparation(self):
        return prepare(root=self.root,source=self.source,loaded=self.loaded,runner=self.runner,
                       control=self.control,path=self.path,context='fixture-context',clock=datetime.now(timezone.utc),progress=lambda _:None)

    def test_million_dollar_preview_splits_limits_and_is_read_only(self):
        result=self.preparation()
        self.assertTrue(result['execution_ready'])
        self.assertEqual(len(result['orders']),16)
        self.assertTrue(all(D(o['notional'])<=100000 for o in result['orders']))
        self.assertEqual(self.account.cash,D(1000000))
        self.assertFalse(list(self.root.rglob('*.sqlite')))

    def test_zero_actionable_whole_shares_is_explicit_noop(self):
        self.account=replace(self.account,cash=D('50'))
        result=self.preparation()
        self.assertEqual(result['status'],'complete')
        self.assertTrue(result['no_orders'])
        self.assertFalse(result['execution_ready'])

    def test_zero_orders_with_actionable_difference_does_not_claim_success(self):
        with patch('app.paper_preparation.plan_orders',return_value=()):
            with self.assertRaisesRegex(ControlError,'整股差额'):
                self.preparation()

    def test_paused_prepared_batch_recovers_after_expiry_with_original_orders(self):
        from app.paper_recovery import recover_prepared
        result=self.preparation(); test=self
        original_quotes=self.control.observe_quotes
        clock=[datetime.now(timezone.utc)]
        saved={};sent=[]
        class Broker:
            mode='broker_paper'
            def account(self):return replace(test.account,as_of=clock[0])
            def execution_time(self):return clock[0]
            def open_orders(self):return ()
            def query(self,ident):return saved[ident]
            def submit(self,order):
                sent.append(order)
                quotes,_,_=test.control.observe_quotes({order.symbol:'unused',**{s:'unused' for s,q in test.account.positions}},120)
                test.account=check_order(order,self.account(),quotes,clock[0],test.runner.policy,test.runner.fees)
                saved[order.client_id]=OrderState(order.client_id,'order-'+order.client_id,'filled',order.quantity,order.limit_price)
                if len(sent)==1:raise ConnectionError('Lost acknowledgment after fill')
                return saved[order.client_id]
        @contextmanager
        def session(*args,**kwargs):
            journal=Journal(self.root/'journal.sqlite')
            try:yield Broker(),journal
            finally:journal.close()
        self.control.session=session
        self.control.mode='broker_paper';self.control.state_dir=self.root
        self.control.reconciliation_session=session
        self.control.observe_quotes=lambda mapping,max_age:(
            {s:Quote(s,D('99.99'),D(100),clock[0]) for s in mapping},self.previous,clock[0])
        with self.assertRaises(ConnectionError):self.execution(result)
        clock[0]+=timedelta(minutes=3)
        if clock[0].astimezone(ZoneInfo('America/New_York')).date()!=self.now.astimezone(ZoneInfo('America/New_York')).date():
            self.skipTest('Fixture would cross session close at current wall-clock time')
        raw=json.loads(Path(result['snapshot_path']).read_text(encoding='utf-8'))
        reply=recover_prepared(root=self.root,raw=raw,context='fixture-context',token=result['preview_token'],
            source=self.source,runner=self.runner,control=self.control,cfg=self.cfg,clock=clock[0])
        self.assertEqual(reply['recovery_state'],'complete')
        self.assertEqual(len(sent),16)
        self.assertEqual(len({o.client_id for o in sent}),16)
        self.assertEqual([str(o.limit_price) for o in sent],[o['limit_price'] for o in raw['orders']])

    def execution(self,result):
        raw=json.loads(Path(result['snapshot_path']).read_text(encoding='utf-8'))
        return execute_prepared(root=self.root,raw=raw,context='fixture-context',token=result['preview_token'],
            source=self.source,runner=self.runner,control=self.control,cfg=self.cfg,clock=datetime.now(timezone.utc))

    def test_full_prepare_to_16_fills_with_cash_covered_broker_double(self):
        result=self.preparation();test=self
        class Broker:
            mode='broker_paper'
            def account(self):return replace(test.account,as_of=datetime.now(timezone.utc))
            def execution_time(self):return datetime.now(timezone.utc)
            def open_orders(self):return ()
            def submit(self,order):
                quotes,_,_=test.control.observe_quotes({order.symbol:'unused',**{s:'unused' for s,q in test.account.positions}},120)
                test.account=check_order(order,self.account(),quotes,self.execution_time(),test.runner.policy,test.runner.fees)
                return OrderState(order.client_id,'test-order-'+order.client_id,'filled',order.quantity,order.limit_price)
        @contextmanager
        def session(cfg,snapshot,fees):
            journal=Journal(self.root/'journal.sqlite')
            try:yield Broker(),journal
            finally:journal.close()
        self.control.session=session
        self.assertEqual(self.execution(result)['status'],'complete')
        self.assertEqual(len(self.account.positions),8)
        self.assertGreater(self.account.cash,0)

    def test_expired_preview_changed_signal_and_changed_account_block(self):
        result=self.preparation();raw=json.loads(Path(result['snapshot_path']).read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ControlError,'过期'):
            original=self.control.observe_quotes
            self.control.observe_quotes=lambda m,a:(*original(m,a)[:2],datetime.now(timezone.utc)+timedelta(minutes=3))
            try:
                execute_prepared(root=self.root,raw=raw,context='fixture-context',token=result['preview_token'],
                    source=self.source,runner=self.runner,control=self.control,cfg=self.cfg,
                    clock=datetime.now(timezone.utc)+timedelta(minutes=3))
            finally:self.control.observe_quotes=original
        self.path.write_text('{"changed":true}')
        with self.assertRaisesRegex(ControlError,'信号文件'):
            self.execution(result)
        raw['orders'][0]['quantity']='1'
        with self.assertRaisesRegex(ControlError,'预览'):
            execute_prepared(root=self.root,raw=raw,context='fixture-context',token=result['preview_token'],
                source=self.source,runner=self.runner,control=self.control,cfg=self.cfg,clock=datetime.now(timezone.utc))

    def test_holding_outside_ranked_prefix_has_sell_mapping(self):
        account=replace(self.account,positions=(('OTHER',D(10)),))
        mapping, observed=mapping_for(self.stocks,account)
        self.assertEqual(mapping['HELD:US:OTHER'],'OTHER')
        self.assertEqual(observed.positions,(('HELD:US:OTHER',D(10)),))
        self.account=account
        result=self.preparation()
        self.assertEqual(result['orders'][0]['ticker'],'OTHER')
        self.assertEqual(result['orders'][0]['side'],'SELL')

    def test_current_metadata_is_never_backdated_for_backtest(self):
        engine=PortfolioEngine(MOM8())
        day=self.now.astimezone(ZoneInfo('America/New_York')).date()
        from framework.portfolio import PortfolioContext
        context=PortfolioContext(day,())
        with self.assertRaises(ValueError):engine.plan(self.stocks,day,context)
        self.assertEqual(len(engine.plan(self.stocks,day,context,current_information=True)),8)
        future=(replace(self.stocks[0],known_on=day+timedelta(days=1)),*self.stocks[1:])
        with self.assertRaises(ValueError):engine.plan(future,day,context,current_information=True)

    def test_changed_account_blocks_before_any_submission(self):
        from test_execution import decision
        current=replace(self.account,cash=self.account.cash-D(1))
        broker=Mock(mode='broker_paper');broker.account.return_value=current
        broker.execution_time.return_value=datetime.now(timezone.utc)
        journal=Journal(self.root/'journal.sqlite')
        try:
            day=datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            with self.assertRaisesRegex(ExecutionBlocked,'Account changed'):
                Executor(broker,journal,self.runner.policy,self.runner.fees).run(decision(day=day),{},self.now,
                    approved_orders=(),expected_account=self.account)
            broker.submit.assert_not_called()
            self.assertIsNone(journal.batch('test-paper:'+day))
        finally:journal.close()

    def test_accepted_order_is_queried_until_filled_without_resubmit(self):
        from platforms.paper import PaperBroker
        from test_execution import decision
        quotes={s:Quote(s,D(99),D(100),self.now) for s in ('A','B')}
        path=self.root/'paper.sqlite';PaperBroker.create(path,D(10000))
        broker=PaperBroker(path,quotes,self.now,Fees())
        proxy=Mock(wraps=broker);proxy.mode='paper'
        def accepted(o):
            filled=broker.submit(o)
            return replace(filled,status='accepted',filled_quantity=D(0),average_price=D(0))
        proxy.submit.side_effect=accepted
        journal=Journal(self.root/'journal.sqlite')
        try:
            day=self.now.astimezone(ZoneInfo('America/New_York')).date().isoformat()
            with patch('framework.execution.time.sleep'):
                result=Executor(proxy,journal,ExecutionPolicy(max_gross=D('.9'),max_weight=D('.45')),Fees(),
                                settlement_seconds=1).run(decision(day=day),quotes,self.now)
            self.assertEqual(result['orders'],2)
            self.assertEqual(proxy.submit.call_count,2)
            self.assertEqual(proxy.query.call_count,2)
        finally:broker.close();journal.close()

    def test_equity_quote_connection_does_not_select_crypto_firm(self):
        from platforms.opend import connection
        class QuoteClient:
            arguments=None
            def __init__(self,**kwargs):QuoteClient.arguments=kwargs
            def get_login_user_id(self):return 7
            def close(self):pass
        cfg=SimpleNamespace(environment='SIMULATE',provider='moomoo',host='127.0.0.1',port=11111,login_user_id='7')
        sdk=SimpleNamespace(OpenQuoteContext=QuoteClient)
        with patch('platforms.opend.load_sdk',return_value=sdk),patch('platforms.opend.socket.create_connection'):
            with connection(cfg):pass
        self.assertNotIn('security_firm',QuoteClient.arguments)


class TestQuotes(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,2,15,0,tzinfo=timezone.utc)
        self.connector=SimpleNamespace(mode='broker_paper',environment='SIMULATE',_settings=lambda:None)
        self.sdk=SimpleNamespace(Market=SimpleNamespace(US='US'),MarketState=SimpleNamespace(MORNING='MORNING',AFTERNOON='AFTERNOON'),
            SecurityType=SimpleNamespace(STOCK='STOCK',ETF='ETF'),SecurityStatus=SimpleNamespace(NORMAL='NORMAL'))
        self.rows=[{'code':'US.AAPL','suspension':False,'sec_status':'NORMAL',
                    'update_time':'2026-10-02 11:00:00','bid_price':99.9,'ask_price':100.}]
        self.states=[{'code':'US.AAPL','market_state':'MORNING'}]
        @contextmanager
        def connection(_):yield self.sdk,object(),{}
        def call(client,method,**kwargs):
            return {'get_market_state':self.states,'request_trading_days':[{'time':'2026-10-01'}],
                    'get_global_state':{'timestamp':datetime.now(timezone.utc).timestamp()},
                    'get_stock_basicinfo':[{'code':'US.AAPL','stock_type':'STOCK','delisting':False}],
                    'get_market_snapshot':self.rows}[method]
        for name,value in [('connection',connection),('records',lambda x:x),('call',call)]:
            p=patch('brokers.opend_observation.'+name,value);p.start();self.addCleanup(p.stop)
        p=patch('brokers.opend_observation.instrument_validator',lambda sdk,client:lambda tickers:None);p.start();self.addCleanup(p.stop)

    def quote(self):return quote_observation(self.connector,{'FIGI:1':'AAPL'},120,now=self.now)
    def test_vendor_time_and_bid_ask_preserved(self):
        quotes,previous,_=self.quote()
        self.assertEqual(quotes['FIGI:1'].as_of.astimezone(timezone.utc),self.now)
        self.assertEqual(quotes['FIGI:1'].bid,D('99.9'))
    def test_stale_zero_price_closed_suspended_and_live_all_block(self):
        for values in ({'update_time':'2026-10-02 10:00:00'},{'bid_price':0},{'suspension':True}):
            original=dict(self.rows[0]);self.rows[0].update(values)
            with self.assertRaises(ControlError):self.quote()
            self.rows[0]=original
        self.states[0]['market_state']='CLOSED'
        with self.assertRaisesRegex(ControlError,'交易时段'):self.quote()
        self.connector.mode='live'
        with self.assertRaises(ControlError):self.quote()
