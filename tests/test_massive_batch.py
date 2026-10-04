"""Batch normalization, exact lazy selection, rolling-cache resume and cancellation."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from app.assembly import strategy_data_requirements
from app.control_contracts import ControlError
from data.common import DataIssue
from data.massive import MassiveClient, MassiveError, Settings, TICKERS, SPLITS, canonical_url
from data.massive_batch import grouped_rows, market_histories, split_events
from data.massive_provider import MassiveData
from data.request_control import MassiveProviderControl
from framework.portfolio import EquitySnapshot, PortfolioContext
from strategies.mom8 import MOM8
from test_massive import Clock, Reply, KEY

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 14, tzinfo=timezone.utc)


def bar(symbol, day, close, volume=2e6):
    return {'T': symbol, 't': datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()*1000+12*3600000,
            'o': close, 'h': close, 'l': close, 'c': close, 'v': volume}


def grouped(rows):
    return {'observed_at': NOW.isoformat(),
            'data': {'adjusted': False, 'resultsCount': len(rows), 'results': rows, 'status': 'OK'}}


class BatchValidationTests(unittest.TestCase):
    def test_rejects_duplicate_wrong_date_missing_reference_count_and_negative_volume(self):
        day = date(2026, 10, 1)
        good = grouped([bar('SPY', day, 100), bar('AAPL', day, 50)])
        self.assertEqual(grouped_rows(good, day, {'AAPL'})['AAPL'], (50, 2e6))
        variants = []
        for key, value in [('resultsCount', 1), ('adjusted', True)]:
            raw = deepcopy(good); raw['data'][key] = value; variants.append(raw)
        for rows in ([bar('AAPL', day, 50)], [bar('SPY', day, 100), bar('SPY', day, 100)],
                     [bar('SPY', day-timedelta(days=1), 100)], [bar('SPY', day, 100, -1)]):
            variants.append(grouped(rows))
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(DataIssue):
                grouped_rows(raw, day, {'AAPL'})

    def test_forward_reverse_and_multiple_splits_reconstruct_signal_day_shares(self):
        days = [date(2026, 9, 28)+timedelta(days=i) for i in range(3)]
        request = strategy_data_requirements({'type': 'mom8'})
        request = replace(request, price_bars=3, volume_bars=3)
        for ratios, closes, volumes, expected in (
                ([(days[1], 2)], [100, 50, 50], [1e6, 2e6, 2e6], 50),
                ([(days[1], .5)], [50, 100, 100], [2e6, 1e6, 1e6], 100),
                ([(days[1], 2), (days[2], 3)], [300, 150, 50], [1e6, 2e6, 6e6], 50)):
            api = Mock(requests=0, hits=0)
            api.pages.return_value = {'observed_at': NOW.isoformat(), 'results': [
                {'ticker': 'AAPL', 'id': str(i), 'execution_date': str(day), 'split_from': 1,
                 'split_to': ratio, 'historical_adjustment_factor': .00001}
                for i, (day, ratio) in enumerate(ratios)]}
            api.grouped_daily.side_effect = lambda day, **kw: grouped([
                bar('SPY', date.fromisoformat(day), 100),
                bar('AAPL', date.fromisoformat(day), closes[days.index(date.fromisoformat(day))],
                    volumes[days.index(date.fromisoformat(day))])])
            result, _, _, issues = market_histories(api, days, ['AAPL'], request, NOW, lambda _: None)
            self.assertEqual(issues, [])
            self.assertEqual(result['AAPL'].adjusted_closes, (expected,)*3)
            self.assertEqual(result['AAPL'].share_volumes, (volumes[-1],)*3)
            self.assertEqual(result['AAPL'].raw_close, closes[-1])
            self.assertEqual(result['AAPL'].dollar_volumes, tuple(p*v for p,v in zip(closes,volumes)))

    def test_split_cutoff_ratios_and_duplicate_events_fail_closed(self):
        first, end = date(2026, 9, 1), date(2026, 10, 1)
        event = {'ticker': 'AAPL', 'id': 'one', 'execution_date': str(end), 'split_from': 1, 'split_to': 2}
        api = Mock()
        for rows in ([event, event], [dict(event, execution_date='2026-10-02')],
                     [dict(event, split_from=0)], [dict(event, split_to=float('nan'))]):
            api.pages.return_value = {'results': rows, 'observed_at': NOW.isoformat()}
            with self.assertRaises(DataIssue): split_events(api, first, end)
        self.assertEqual(api.pages.call_args.args[1]['execution_date.lte'], str(end))
        canonical_url(SPLITS)
        canonical_url('/v2/aggs/grouped/locale/us/market/stocks/2026-10-01')
        with self.assertRaises(MassiveError): canonical_url('/v1/orders')

    def test_same_day_multiple_actions_use_vendor_adjustment_without_guessing(self):
        days = [date(2026,9,28)+timedelta(days=i) for i in range(3)]
        request = replace(strategy_data_requirements({'type':'mom8'}),price_bars=3,volume_bars=3)
        api = Mock(requests=0,hits=0)
        api.pages.return_value = {'observed_at':NOW.isoformat(),'results':[
            {'ticker':'AAPL','id':str(i),'execution_date':str(days[1]),'split_from':1,'split_to':ratio}
            for i,ratio in enumerate((2,.5))]}
        api.grouped_daily.side_effect = lambda day,**kw: grouped([
            bar('SPY',date.fromisoformat(day),100),bar('AAPL',date.fromisoformat(day),50)])
        api.daily.return_value = {'observed_at':NOW.isoformat(),'data':{'results':[
            {k:v for k,v in bar('AAPL',day,50).items() if k!='T'} for day in days]}}
        rows, _, _, issues=market_histories(api,days,['AAPL'],request,NOW,lambda _:None)
        self.assertEqual(issues,[])
        self.assertEqual(rows['AAPL'].adjusted_closes,(50,50,50))
        api.daily.assert_called_once_with('AAPL',str(days[0]),str(days[-1]))


class BatchCollectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root, self.clock = Path(temp.name), Clock()
        self.cfg = {'type': 'mom8', 'lookback': 20, 'skip': 2, 'trend_window': 20}
        self.strategy = MOM8(**{k:v for k,v in self.cfg.items() if k!='type'})
        self.request = strategy_data_requirements(self.cfg)
        self.days = [date(2026, 9, 2)+timedelta(days=i) for i in range(30)
                     if (date(2026, 9, 2)+timedelta(days=i)).weekday()<5][-21:]
        self.symbols = ['S'+str(i).zfill(2) for i in range(40)]
        self.opener = Mock(); self.opener.open.side_effect = self.respond
        self.clients = []
        self.details = []

    def detail(self, symbol):
        index = int(symbol[1:]) if symbol.startswith('S') else 99
        return {'ticker': symbol, 'type': 'CS', 'locale': 'us', 'market': 'stocks', 'active': True,
                'primary_exchange': 'XNAS', 'currency_name': 'usd', 'list_date': '2000-01-01',
                'share_class_figi': 'ID'+symbol, 'market_cap': 1e9 if index==0 else 10e9,
                'sic_code': str(4000+index//4), 'cik': '123'}

    def close(self, symbol, index):
        if symbol=='SPY': return 100+index/2
        if symbol=='AAPL': return 1  # preflight probe is not an eligible candidate
        rank = int(symbol[1:])
        if index==len(self.days)-3: return 200-rank*2
        return 200 if index==len(self.days)-1 else 100

    def respond(self, req, timeout):
        part = urlsplit(req.full_url)
        if part.path==TICKERS:
            raw = {'results': [self.detail(s) for s in self.symbols], 'status': 'OK'}
        elif part.path.startswith(TICKERS+'/'):
            symbol=part.path.rsplit('/',1)[-1]; self.details.append(symbol)
            raw={'results':self.detail(symbol),'status':'OK'}
        elif part.path==SPLITS:
            raw={'results':[],'status':'OK'}
        elif part.path.startswith('/v2/aggs/grouped/'):
            self.assertEqual(parse_qs(part.query), {'adjusted':['false'],'include_otc':['false']})
            day=date.fromisoformat(part.path.rsplit('/',1)[-1]); index=self.days.index(day)
            rows=[bar(s,day,self.close(s,index)) for s in ('SPY',*self.symbols)]
            raw=grouped(rows)['data']
        elif part.path.startswith('/v2/aggs/ticker/SPY/'):
            raw={'ticker':'SPY','adjusted':True,'results':[
                {k:v for k,v in bar('SPY',d,self.close('SPY',i)).items() if k!='T'} for i,d in enumerate(self.days)]}
        else: raise AssertionError('Unexpected endpoint: '+part.path)
        return Reply(raw, req.full_url)

    def client(self, cfg, path, **kwargs):
        api=MassiveClient(cfg,path,opener=self.opener,clock=self.clock.now,
                          monotonic=self.clock.monotonic,sleep=self.clock.sleep,**kwargs)
        self.clients.append(api)
        return api

    def collect(self, name, progress=None, request=None):
        collector=MassiveData(self.root/'secrets',ROOT/'configs/massive.json',self.root/'cache').for_request(request or self.request)
        with patch('data.massive_provider.MassiveClient',side_effect=self.client), \
             patch('data.massive_provider.settings',return_value=Settings(KEY,5)), \
             patch('data.massive_provider.utcnow',side_effect=self.clock.now):
            path=collector.collect(self.root/name,progress=progress)
        return json.loads(path.read_text(encoding='utf-8'))

    def full_stocks(self):
        return tuple(EquitySnapshot('FIGI:ID'+s,s,self.days[-1],self.days[-1],
            'SIC:'+self.detail(s)['sic_code'],0,tuple(self.close(s,i) for i in range(len(self.days))),(),
            self.detail(s)['market_cap'],200,(2e6,)*20) for s in self.symbols)

    def test_lazy_prefix_equals_full_universe_including_rank12_retention_and_industry_limits(self):
        raw=self.collect('one')
        loaded=MassiveProviderControl(self.root,{},{}).for_request(self.request).load(raw)
        eager=self.full_stocks()
        ranked=self.strategy.rank(eager)
        self.assertEqual(len(ranked),12)
        held=ranked[-1][1].asset_id
        context=PortfolioContext(self.days[-1],(held,))
        self.assertEqual(self.strategy.select_with_context(loaded.stocks,context),
                         self.strategy.select_with_context(eager,context))
        self.assertEqual([(score,s.asset_id) for score,s in self.strategy.rank(loaded.stocks)],
                         [(score,s.asset_id) for score,s in ranked])
        self.assertEqual(len(self.strategy.research_targets(loaded.stocks)),8)
        self.assertTrue(raw['collection_complete'])
        self.assertTrue(raw['coverage']['ranked_prefix_complete'])
        self.assertGreater(raw['coverage']['details_remaining'],0)
        self.assertNotIn('S39',self.details)
        # 21 whole-market days, one batch request per day, never two histories per stock.
        self.assertEqual(sum('/aggs/grouped/' in c.args[0].full_url for c in self.opener.open.call_args_list),21)
        self.assertFalse(any('/aggs/ticker/S' in c.args[0].full_url and '/SPY/' not in c.args[0].full_url
                             for c in self.opener.open.call_args_list))

    def test_cache_reuse_new_trading_day_and_corruption_revalidation(self):
        self.collect('one')
        raw=self.collect('two')
        self.assertEqual(raw['network_requests'],4)  # live permission probes only
        self.assertGreater(raw['cache_hits'],21)
        self.days=self.days[1:]+[date(2026,10,2)]
        self.clock.sleep(18*3600)  # past NY midnight; completed signal day advances
        before=self.opener.open.call_count
        raw=self.collect('three')
        requests=[c.args[0].full_url for c in self.opener.open.call_args_list[before:]]
        self.assertEqual([urlsplit(u).path.rsplit('/',1)[-1] for u in requests if '/aggs/grouped/' in u],['2026-10-02'])
        self.assertEqual(raw['network_requests'],4)
        old_date=str(self.days[0])
        for path in (self.root/'cache').rglob('*.json'):
            saved=json.loads(path.read_text())
            if '/aggs/grouped/' in saved.get('request','') and old_date in saved['request']:
                saved['data']['results'][0]['c']=.01
                path.write_text(json.dumps(saved))
                break
        before=self.opener.open.call_count
        self.collect('four')
        requests=[c.args[0].full_url for c in self.opener.open.call_args_list[before:]]
        self.assertTrue(any('/aggs/grouped/' in u and old_date in u for u in requests))

    def test_cancel_preserves_cached_days_and_resume_fills_remaining(self):
        def progress(message):
            if message.startswith('批量日线 3/'):
                (self.root/'cache/cancel.request').write_text('stop')
        with self.assertRaises(MassiveError) as error:self.collect('one',progress)
        self.assertEqual(error.exception.code,'cancelled')
        self.assertFalse((self.root/'one/preview-data.json').exists())
        self.assertGreater(len(list((self.root/'cache').rglob('*.json'))),3)
        before=self.opener.open.call_count
        raw=self.collect('two')
        fetched=[urlsplit(c.args[0].full_url).path.rsplit('/',1)[-1]
                 for c in self.opener.open.call_args_list[before:] if '/aggs/grouped/' in c.args[0].full_url]
        self.assertTrue(raw['collection_complete'])
        self.assertFalse(any(str(day) in fetched for day in self.days[:3]))

    def test_exhausts_candidates_when_industry_caps_cannot_fill_buffer(self):
        policy=replace(self.request.collection_policy,complete=lambda stocks: False)
        request=replace(self.request,collection_policy=policy)
        raw=self.collect('one',request=request)
        self.assertFalse(raw['coverage']['ranked_prefix_complete'])
        self.assertTrue(raw['coverage']['all_candidates_checked'])
        self.assertEqual(raw['coverage']['details_remaining'],0)

    def test_changed_strategy_screen_or_unfinished_prefix_cannot_reuse_snapshot(self):
        raw=self.collect('one')
        request=strategy_data_requirements(dict(self.cfg,min_price=6))
        self.assertNotEqual(request.fingerprint,self.request.fingerprint)
        with self.assertRaises(ControlError):MassiveProviderControl(self.root,{},{}).for_request(request).load(raw)
        raw['coverage'].update(ranked_prefix_complete=False,all_candidates_checked=False)
        with self.assertRaises(ControlError):MassiveProviderControl(self.root,{},{}).for_request(self.request).load(raw)


if __name__=='__main__':unittest.main()
