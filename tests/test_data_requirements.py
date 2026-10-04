"""Provider-independent requests and actual endpoint selection; no network/trades."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.assembly import build_portfolio_runner, strategy_data_requirements
from app.control_contracts import ControlError
from app.module_registry import ModuleRegistry
from app.portal_worker import research_preview, perform
from data.common import DataIssue, write_json
from data.massive import INCOME, TICKERS, SPLITS, MassiveError, Settings
from data.massive_provider import MassiveData
from data.request_control import MassiveProviderControl
from framework.configuration import load_config
from portal.service import PortalService

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,10,2,12,tzinfo=timezone.utc)


class DataRequirementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cfg=load_config(ROOT/'configs/portfolio_config.json')
        self.mom=load_config(ROOT/'configs/mom8.json')
        self.days=sorted(date(2026,10,1)-timedelta(days=i) for i in range(600)
                         if (date(2026,10,1)-timedelta(days=i)).weekday()<5)[-253:]
        self.api=Mock(requests=8,hits=0)
        self.api.request.side_effect=self.detail
        self.api.pages.side_effect=self.pages
        self.api.daily.side_effect=self.daily
        self.api.grouped_daily.side_effect=self.grouped

    def detail(self,path,**kwargs):
        symbol=path.rsplit('/',1)[-1]
        return {'observed_at':NOW.isoformat(),'data':{'results':{
            'ticker':symbol,'type':'CS','market':'stocks','locale':'us','active':True,
            'currency_name':'usd','primary_exchange':'XNAS','cik':'123',
            'sic_code':'6021' if symbol=='BANK' else '7372','share_class_figi':'FIGI'+symbol,
            'market_cap':3e10,'list_date':'2000-01-01'}}}

    def pages(self,path,params,**kwargs):
        if path==TICKERS:
            rows=[self.detail(TICKERS+'/'+s)['data']['results'] for s in ['AAPL','MSFT','BANK']]
        elif path==INCOME:
            rows=[{'tickers':[params['tickers']],'timeframe':'trailing_twelve_months','cik':'123',
                   'period_end':'2026-06-30','filing_date':'2026-08-01','fiscal_quarter':2,'operating_income':1e9}]
        elif path==SPLITS:
            rows=[]
        else: raise AssertionError('Unexpected endpoint')
        return {'results':rows,'observed_at':NOW.isoformat()}

    def daily(self,symbol,start,end,adjusted=True):
        return {'observed_at':NOW.isoformat(),'data':{'results':[
            {'t':datetime.combine(d,datetime.min.time(),tzinfo=timezone.utc).timestamp()*1000+12*3600000,
             'c':100+i/2,'v':2e6} for i,d in enumerate(self.days)]}}

    def grouped(self,day,**kwargs):
        day=date.fromisoformat(day)
        index=self.days.index(day)
        price=100+index/2
        rows=[{'T':s,'t':datetime.combine(day,datetime.min.time(),tzinfo=timezone.utc).timestamp()*1000+12*3600000,
               'o':price,'h':price,'l':price,'c':price,'v':2e6} for s in ('SPY','AAPL','MSFT','BANK')]
        return {'observed_at':NOW.isoformat(),'data':{'adjusted':False,'resultsCount':len(rows),'results':rows}}

    def collect(self,cfg=None):
        cfg=cfg or self.cfg
        request=strategy_data_requirements(cfg['strategy'])
        collector=MassiveData(self.root/'secrets',ROOT/'configs/massive.json',self.root/'cache').for_request(request)
        with patch('data.massive_provider.MassiveClient',return_value=self.api), \
             patch('data.massive_provider.settings',return_value=Settings('test-key-never-use')), \
             patch('data.massive_provider.utcnow',return_value=NOW):
            path=collector.collect(self.root/'out')
        return path,json.loads(path.read_text(encoding='utf-8')),request

    def control(self,request):
        return MassiveProviderControl(self.root,{},{}).for_request(request)

    def test_technology_only_downloads_history_and_financials_for_selected_sector(self):
        _,raw,request=self.collect()
        self.assertEqual({s['ticker'] for s in raw['stocks']},{'AAPL','MSFT'})
        self.assertEqual(raw['candidate_count'],2)
        self.assertNotIn('BANK',[c.args[0] for c in self.api.daily.call_args_list])
        self.assertNotIn('BANK',[c.args[1].get('tickers') for c in self.api.pages.call_args_list])
        self.assertNotIn('market_cap',raw['stocks'][0])
        self.assertTrue(raw['collection_complete'])
        self.assertEqual(self.control(request).load(raw).schema,'us_technology_daily_v1')

    def test_mom8_requests_all_sectors_and_never_calls_financials(self):
        path,raw,request=self.collect(self.mom)
        self.assertEqual({s['ticker'] for s in raw['stocks']},{'AAPL','MSFT','BANK'})
        self.assertNotIn(INCOME,[c.args[0] for c in self.api.pages.call_args_list])
        self.assertEqual({c.args[0] for c in self.api.daily.call_args_list},{'SPY'})
        self.assertEqual(self.api.grouped_daily.call_count,254)
        self.assertTrue(all('operating_income_ttm' not in s for s in raw['stocks']))
        loaded=self.control(request).load(raw)
        preview=research_preview(self.mom,build_portfolio_runner(self.mom),loaded,path)
        self.assertFalse(preview['execution_ready'])
        self.assertNotIn('preview_token',preview)
        self.assertIn('首次建仓',preview['details']['assumptions'][0])

    def test_disabling_profit_gate_removes_financial_requests_without_changing_scope(self):
        self.cfg['strategy']['require_profit']=False
        _,raw,request=self.collect()
        self.assertNotIn(INCOME,[c.args[0] for c in self.api.pages.call_args_list])
        self.assertEqual({s['ticker'] for s in raw['stocks']},{'AAPL','MSFT'})
        self.assertNotIn('operating_income_ttm',request.fields)
        self.assertTrue(build_portfolio_runner(self.cfg).engine.strategy.select(self.control(request).load(raw).stocks))

    def test_missing_financial_permission_stops_before_universe_and_is_not_optional(self):
        def pages(path,params,**kwargs):
            if path==INCOME: raise MassiveError('entitlement','financials unavailable')
            return self.pages(path,params,**kwargs)
        self.api.pages.side_effect=pages
        with self.assertRaises(MassiveError):self.collect()
        self.assertNotIn(TICKERS,[c.args[0] for c in self.api.pages.call_args_list])
        self.assertFalse((self.root/'out/preview-data.json').exists())

    def test_missing_required_field_cannot_be_filled_with_zero(self):
        _,raw,request=self.collect()
        del raw['stocks'][0]['operating_income_ttm']
        with self.assertRaises(ControlError):self.control(request).load(raw)

    def test_wrong_scope_changed_history_or_missing_price_bar_rejects_snapshot(self):
        _,raw,request=self.collect()
        for changed in (replace(request,universe='all'),replace(request,price_bars=126),
                        replace(request,fields=tuple(f for f in request.fields if f!='operating_income_ttm'))):
            with self.subTest(request=changed),self.assertRaises(ControlError):self.control(changed).load(raw)
        raw['stocks'][0]['adjusted_closes'].pop()
        with self.assertRaises(ControlError):self.control(request).load(raw)

    def test_history_comes_from_strategy_parameters(self):
        self.cfg['strategy'].update(lookback=125,skip=10)
        _,raw,request=self.collect()
        self.assertEqual(request.price_bars,126)
        self.assertEqual(len(raw['stocks'][0]['adjusted_closes']),126)
        self.assertEqual(len(raw['stocks'][0]['dollar_volumes']),20)

    def test_unsupported_demand_fails_before_any_vendor_client(self):
        request=strategy_data_requirements(self.cfg['strategy'])
        with patch('data.massive_provider.MassiveClient') as api:
            for changed in (replace(request,fields=request.fields+('order_book',)),
                            replace(request,market='hk'),replace(request,price_bars=1000)):
                with self.assertRaises(DataIssue):MassiveData('x','y','z').for_request(changed)
            api.assert_not_called()

    def test_single_provider_is_compatible_with_both_strategies_and_shared_credentials(self):
        registry=ModuleRegistry(ROOT)
        registry.compatible(self.cfg,'massive')
        registry.compatible(self.mom,'massive')
        items=[x for x in registry.descriptors('data') if x['type'].startswith('massive')]
        self.assertEqual([x['id'] for x in items],['massive'])
        self.assertEqual(registry.canonical_id('data','massive_momentum'),'massive')
        first=registry.control('data','massive',config=self.cfg)
        second=registry.control('data','massive',config=self.mom)
        self.assertEqual(first.profile['config_dir'],second.profile['config_dir'])
        self.assertNotEqual(first.directory,second.directory)
        self.assertEqual(first.profile['cache_dir'],second.profile['cache_dir'])

    def test_data_request_does_not_change_when_only_position_count_changes(self):
        first=strategy_data_requirements(self.cfg['strategy'])
        self.cfg['strategy']['count']=4
        self.assertEqual(first.fingerprint,strategy_data_requirements(self.cfg['strategy']).fingerprint)

    def test_local_file_source_accepts_both_formats_and_rejects_wrong_schema(self):
        from data.snapshot import demo_snapshot
        from tests.test_mom8 import snapshot
        registry=ModuleRegistry(ROOT)
        tech=registry.control('data','snapshot_file',config=self.cfg)
        mom=registry.control('data','snapshot_file',config=self.mom)
        self.assertFalse(tech.load(demo_snapshot()).research_only)
        self.assertFalse(mom.load(snapshot()).research_only)
        with self.assertRaises(ControlError):mom.load(demo_snapshot())

    def test_switching_strategy_retains_provider_and_clears_old_snapshot(self):
        for folder in ('configs','strategies','framework','data','brokers'):
            shutil.copytree(ROOT/folder,self.root/folder,ignore=shutil.ignore_patterns('__pycache__'))
        prefs=self.root/'runs/portal/preferences.json'
        write_json(prefs,{'source':'massive_momentum','broker':'paper'})
        with patch('app.assembly.ROOT',self.root):
            service=PortalService(self.root)
            self.assertEqual(service.source,'massive')
            service.selected_snapshot='old-selection'
            before=service.registry.control('data','massive').directory
            cfg,version=service.config()
            service.change_strategy(self.mom['strategy'],version)
            self.assertEqual(service.source,'massive')
            self.assertIsNone(service.selected_snapshot)
            self.assertNotEqual(before,service.registry.control('data','massive').directory)
            self.assertEqual(service.state()['data_requirements']['universe'],'all')
            self.assertIsNone(service.state()['compatibility_issue'])
            self.assertFalse((self.root/'runs/portal-local-paper').exists())


if __name__=='__main__':unittest.main()
