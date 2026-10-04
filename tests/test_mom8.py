"""MOM8 selection, data boundaries, repeat-safe replay and QC export parity."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.assembly import build_portfolio_runner, portfolio_spec
from bot import export_backtest, run_paper
from app.control_contracts import ControlError
from data.equity_momentum import SCHEMA, RESEARCH_SCHEMA, parse_equity_snapshot
from data.massive import TICKERS, Settings
from data.massive_momentum import MassiveMomentumData
from framework.configuration import load_config
from framework.portfolio import EquitySnapshot, PortfolioContext, PortfolioEngine, StockSnapshot
from app.module_registry import ModuleRegistry
from app.portal_worker import research_preview
from strategies.mom8 import MOM8
from test_composition import bundle_namespace

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 1)
SIGNAL = date(2026, 8, 31)


def equity(i=0, **changes):
    prices = [100.0] * 253
    prices[-22] = 200 - i * 2
    prices[-1] = 200.0
    item = EquitySnapshot(f'S{i:02}', f'S{i:02}', SIGNAL, SIGNAL, str(i // 2), 0.0,
        tuple(prices), (20_000_000.0,) * 20, 10e9, 50.0, (2_000_000.0,) * 20)
    return replace(item, **changes)


def snapshot():
    rows = []
    stamp = '2026-09-01T09:35:00-04:00'
    for i in range(14):
        raw = asdict(equity(i))
        raw.update(known_on=str(SIGNAL), price_as_of=str(SIGNAL))
        rows.append(raw)
    return {'input_schema': SCHEMA, 'universe_complete': True, 'synthetic': True,
        'universe_scope': 'us_listed_common_stock', 'execution_time': stamp, 'stocks': rows,
        'portfolio_context': {'as_of': str(DAY), 'held_asset_ids': []},
        'quotes': [{'asset_id': s['asset_id'], 'bid': '49.99', 'ask': '50.01', 'as_of': stamp} for s in rows]}


class MOM8Tests(unittest.TestCase):
    def setUp(self):
        self.strategy = MOM8()
        self.rows = [equity(i) for i in range(16)]
        self.cfg = load_config(ROOT / 'configs/mom8.json')

    def test_skip_month_trend_price_cap_share_volume_and_no_profit_gate(self):
        good = equity(operating_income_ttm=-1e9)
        bad = [equity(1, market_cap=1e9), equity(2, market_cap=501e9),
               equity(3, raw_close=4.99), equity(4, share_volumes=(999999.0,) * 20),
               equity(5, adjusted_closes=(100.0,) * 253), equity(6, industry='')]
        target, = self.strategy.select_with_context([good, *bad], PortfolioContext(DAY, ()))
        self.assertEqual(target.asset_id, 'S00')
        self.assertEqual(target.score, 1.0)
        prices = list(good.adjusted_closes)
        prices[-21:] = [300.0] * 21
        self.assertEqual(self.strategy.research_targets([replace(good, adjusted_closes=tuple(prices))])[0].score, 1)
        self.assertEqual(self.strategy.research_targets([equity(share_volumes=(1e6,) * 20)])[0].asset_id, 'S00')

    def test_diversified_keep12_retains_rank11_not_rank13_and_fills_to_eight(self):
        ranked = self.strategy.rank([replace(s, industry=str(i // 4)) for i, s in enumerate(self.rows)])
        self.assertEqual([s.asset_id for _, s in ranked][:6], ['S00', 'S01', 'S04', 'S05', 'S08', 'S09'])
        context = PortfolioContext(DAY, ('S10', 'S12'))
        selected = self.strategy.select_with_context(list(reversed(self.rows)), context)
        self.assertEqual([t.asset_id for t in selected], ['S10', 'S00', 'S01', 'S02', 'S03', 'S04', 'S05', 'S06'])
        self.assertAlmostEqual(sum(t.weight for t in selected), .95)
        self.assertTrue(all(t.weight == .11875 for t in selected))

    def test_empty_slots_cash_and_negative_momentum_not_extra_filter(self):
        prices = [100.0] * 253
        prices[-22], prices[-1] = 90.0, 150.0
        target, = self.strategy.research_targets([equity(adjusted_closes=tuple(prices))])
        self.assertAlmostEqual(target.score, -.1)
        self.assertEqual(target.weight, .11875)

    def test_tech_schema_missing_or_excess_context_and_invalid_parameters_block(self):
        tech = StockSnapshot(*list(asdict(equity()).values())[:8])
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.strategy.research_targets([tech])
        with self.assertRaisesRegex(ValueError, 'requires portfolio context'):
            PortfolioEngine(self.strategy).plan(self.rows, DAY)
        with self.assertRaisesRegex(ValueError, 'More retained'):
            self.strategy.select_with_context(self.rows, PortfolioContext(DAY, tuple(s.asset_id for s in self.rows[:9])))
        for params in ({'weight': True}, {'count': 0}, {'keep_buffer': 7}, {'trend_window': 254},
                       {'skip': 252}, {'weight': float('nan')}, {'min_price': -5}, {'min_market_cap': 1e12}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                MOM8(**params)

    def test_qc_bundle_has_identical_selection_context_and_fingerprint(self):
        local = build_portfolio_runner(self.cfg)
        market = parse_equity_snapshot(snapshot())
        market = replace(market, portfolio_context=PortfolioContext(DAY, ('S10',)))
        decision = local.plan(market)
        cloud = bundle_namespace(self.cfg)
        strategy = cloud['build_portfolio_strategy'](self.cfg['strategy'])
        engine = cloud['PortfolioEngine'](strategy, .95, .19)
        context = cloud['PortfolioContext'](DAY, ('S10',))
        remote = cloud['make_decision'](engine, market.stocks, DAY, context)
        self.assertEqual(remote.record, decision.record)
        self.assertEqual(remote.decision_id, decision.decision_id)
        self.assertEqual(cloud['_QC_PROFILE']['universe'], 'us_common_stock')

    def test_qc_universe_includes_nontech_and_excludes_adr_etf_otc(self):
        cloud = bundle_namespace(self.cfg)
        def fundamental(name, **changes):
            ref = SimpleNamespace(security_type='ST00000001', is_depositary_receipt=False, is_primary_share=True)
            for k, v in changes.items():
                setattr(ref, k, v)
            return SimpleNamespace(has_fundamental_data=True, symbol=SimpleNamespace(id=name), security_reference=ref,
                asset_classification=SimpleNamespace(morningstar_sector_code=103, morningstar_industry_group_code=123),
                company_reference=SimpleNamespace(primary_exchange_id='NYS'), market_cap=10e9)
        bank, adr, etf, otc = fundamental('BANK'), fundamental('ADR', is_depositary_receipt=True), \
            fundamental('ETF', security_type='ETF'), fundamental('OTC')
        otc.company_reference.primary_exchange_id = 'PINX'
        algo = cloud['TechProfitMomentumAlgorithm']()
        facts = algo._freeze_facts([bank, adr, etf, otc])
        self.assertEqual(list(facts), ['BANK'])
        self.assertEqual(facts['BANK'][3], 10e9)

    def test_executable_schema_requires_full_universe_and_explicit_holdings(self):
        for key, value in [('universe_scope', 'technology'), ('universe_complete', False),
                           ('portfolio_context', None), ('execution_allowed', False)]:
            raw = snapshot()
            raw[key] = value
            with self.subTest(key=key), self.assertRaises(ControlError):
                parse_equity_snapshot(raw)

    def test_qc_volume_filter_uses_signal_day_shares_not_future_split_units(self):
        cloud = bundle_namespace(self.cfg)
        cloud['DataNormalizationMode'] = SimpleNamespace(ADJUSTED='adjusted', RAW='raw', SPLIT_ADJUSTED='split')
        days = sorted(SIGNAL-timedelta(days=i) for i in range(500) if (SIGNAL-timedelta(days=i)).weekday() < 5)[-253:]
        symbol, reference = Mock(value='BANK'), Mock(value='QQQ')
        algo = cloud['TechProfitMomentumAlgorithm']()
        algo.qqq, algo.time = reference, datetime(2026, 9, 1)
        frames = {
            'adjusted': {symbol: {d: (100+i/10, 1e6) for i, d in enumerate(days)},
                         reference: {d: (100.0, 1e6) for d in days}},
            'raw': {symbol: {d: (100.0, 1e6) for d in days[-20:]}},
            'split': {symbol: {d: (50.0, 2e6) for d in days[-20:]}}
        }
        algo._history = lambda symbols, count, mode: frames[mode]
        algo._daily_rows = lambda history, sym, cutoff: history[sym]
        rows = algo._load_candidates({'BANK': (symbol, '123', 0.0, 10e9)}, SIGNAL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].share_volumes, (1e6,) * 20)
        self.assertEqual(rows[0].raw_close, 100)
        self.assertEqual(rows[0].market_cap, 10e9)

    def test_local_replay_buys_eight_without_exceeding_cap_and_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            path.write_text(json.dumps(snapshot()), encoding='utf-8')
            result = run_paper(path, ROOT / 'configs/mom8.json', Path(folder) / 'account')
            repeated = run_paper(path, ROOT / 'configs/mom8.json', Path(folder) / 'account')
            self.assertEqual(result, repeated)
            self.assertEqual(len(result['account']['positions']), 8)
            self.assertGreaterEqual(float(result['account']['cash']), 4900)

    def test_module_discovery_risk_validation_and_incompatible_data(self):
        registry = ModuleRegistry(ROOT)
        self.assertTrue(next(x for x in registry.strategies() if x['type'] == 'mom8')['enabled'])
        registry.validate_strategy(self.cfg)
        registry.compatible(self.cfg, 'massive')
        with self.assertRaises(ControlError):
            registry.compatible(self.cfg, 'futu')
        invalid = deepcopy(self.cfg)
        invalid['strategy']['weight'] = .125
        with self.assertRaises(ControlError):
            registry.validate_strategy(invalid)
        with tempfile.TemporaryDirectory() as folder:
            result = export_backtest(ROOT / 'configs/mom8.json', Path(folder) / 'qc')
            self.assertEqual(result['status'], 'exported_not_run')


class MomentumCollectorTests(unittest.TestCase):
    def test_full_universe_includes_bank_no_income_request_and_preview_is_nonexecutable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collector = MassiveMomentumData(root / 'secrets', ROOT / 'configs/massive_momentum.json', root / 'cache')
            days = sorted(SIGNAL - timedelta(days=i) for i in range(500) if (SIGNAL-timedelta(days=i)).weekday() < 5)[-253:]
            now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            api = Mock(requests=3, hits=0)
            def detail(path, **kwargs):
                symbol = path.rsplit('/', 1)[-1]
                return {'observed_at': now.isoformat(), 'data': {'results': {
                    'ticker': symbol, 'type': 'CS', 'locale': 'us', 'market': 'stocks', 'active': True,
                    'currency_name': 'usd', 'primary_exchange': 'XNYS', 'sic_code': '6021',
                    'share_class_figi': symbol, 'market_cap': 5e9, 'list_date': '2000-01-01'}}}
            def daily(ticker, start, end, adjusted=True):
                values = []
                for i, day in enumerate(days):
                    values.append({'t': datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()*1000 + 12*3600000,
                        'c': 100 + i / 10, 'v': 2000000})
                return {'observed_at': now.isoformat(), 'data': {'results': values}}
            api.pages.return_value = {'results': [detail(TICKERS+'/BANK')['data']['results']]}
            api.request.side_effect, api.daily.side_effect = detail, daily
            with patch('data.massive_momentum.MassiveClient', return_value=api), \
                 patch('data.massive_momentum.settings', return_value=Settings('test-key-000000000000')), \
                 patch('data.massive_momentum.utcnow', return_value=now):
                path = collector.collect(root / 'out')
            raw = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual([s['ticker'] for s in raw['stocks']], ['BANK'])
            self.assertTrue(raw['collection_complete'])
            self.assertFalse(raw['execution_allowed'])
            self.assertEqual(raw['schema'], RESEARCH_SCHEMA)
            self.assertEqual(api.pages.call_count, 1)  # Only common-stock universe, no financials.
            from data.equity_momentum import MassiveMomentumControl
            entry = ModuleRegistry(ROOT).catalog('data')['massive_momentum']
            control = MassiveMomentumControl(ROOT, {}, entry)
            loaded = control.load(raw)
            cfg = load_config(ROOT / 'configs/mom8.json')
            preview = research_preview(cfg, build_portfolio_runner(cfg), loaded, path)
            self.assertFalse(preview['execution_ready'])
            self.assertIn('首次建仓', preview['details']['assumptions'][0])
            self.assertEqual(len(preview['targets']), 1)
            self.assertNotIn('preview_token', preview)


if __name__ == '__main__':
    unittest.main()
