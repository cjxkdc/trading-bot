import json
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch
import assembly
from assembly import build_portfolio_strategy, portfolio_spec, strategy_catalog
from bot import run_paper
from framework.decisions import decide
from framework.portfolio import PortfolioEngine
from platforms.snapshot_data import demo_snapshot
from test_tech_profit_momentum import stock, load_adapter
from tools.build_quantconnect import build


class PluginReplayTests(unittest.TestCase):
    def test_two_portfolio_plugins_use_one_unchanged_engine(self):
        stocks = [stock('A', .1), stock('B', .5)]
        a = PortfolioEngine(build_portfolio_strategy({'type':'tech_profit_momentum'})).plan(stocks, date(2020,1,2))
        b = PortfolioEngine(build_portfolio_strategy({'type':'equal_weight'})).plan(stocks, date(2020,1,2))
        self.assertEqual([t.asset_id for t in a], ['B','A'])
        self.assertEqual([t.asset_id for t in b], ['A','B'])

    def test_new_manifest_registers_a_plugin_without_editing_engine_or_assembly(self):
        module = types.ModuleType('strategies.custom_test_plugin')
        class Custom:
            def __init__(self, answer=7):
                self.answer = answer
            def select(self, stocks):
                return ()
        module.Custom = Custom
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'strategies/plugins').mkdir(parents=True)
            (root/'strategies/plugins/custom.json').write_text(json.dumps({
                'type':'custom', 'kind':'portfolio', 'version':'1',
                'entrypoint':'strategies.custom_test_plugin:Custom'}))
            with patch.object(assembly, 'ROOT', root), patch.dict(sys.modules, {module.__name__: module}):
                plugin = build_portfolio_strategy({'type':'custom', 'answer':42})
                self.assertEqual(plugin.answer,42)
                self.assertEqual(PortfolioEngine(plugin).plan([],date(2020,1,2)),())

    def test_single_asset_plugin_cannot_be_sent_to_portfolio_runner(self):
        with self.assertRaisesRegex(ValueError,'single'):
            build_portfolio_strategy({'type':'ma','fast':20,'slow':60})

    def test_qc_and_local_decision_records_match_for_identical_input(self):
        adapter = load_adapter()
        config = adapter.strategy_configuration()
        stocks = [stock('B'),stock('A')]
        engine = PortfolioEngine(build_portfolio_strategy(config))
        local = decide(engine,stocks,date(2020,1,2),portfolio_spec(config))
        cloud = adapter.make_decision(engine,stocks,date(2020,1,2))
        self.assertEqual(local,cloud)
        self.assertEqual(local,decide(engine,list(reversed(stocks)),date(2020,1,2),portfolio_spec(config)))

    def test_exported_bundle_really_executes_both_plugins_without_local_imports(self):
        api = types.ModuleType('AlgorithmImports')
        api.QCAlgorithm = type('QCAlgorithm',(),{})
        namespace = {'__name__':'bundled_test'}
        with patch.dict(sys.modules,{'AlgorithmImports':api}):
            exec(compile(build(),'qc_bundle','exec'),namespace)
        for strategy in ('tech_profit_momentum','equal_weight'):
            namespace['STRATEGY_TYPE'] = strategy
            config = namespace['strategy_configuration']()
            stocks = [stock('A'),stock('B')]
            engine = PortfolioEngine(build_portfolio_strategy(config))
            local = decide(engine,stocks,date(2020,1,2),portfolio_spec(config))
            cloud = namespace['make_decision'](engine,stocks,date(2020,1,2))
            self.assertEqual(local.decision_id,cloud.decision_id)
            self.assertEqual(local.record,cloud.record)

    def test_mixed_signal_sessions_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError,'same completed'):
            decide(PortfolioEngine(build_portfolio_strategy({'type':'equal_weight'})),
                   [stock('A'),stock('B',price_as_of=date(2019,12,30),known_on=date(2019,12,30))],
                   date(2020,1,2),{'type':'equal_weight'})

    def test_cli_replay_is_persistent_and_lost_journal_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/'snapshot.json'
            path.write_text(json.dumps(demo_snapshot()))
            config = Path(__file__).resolve().parents[1]/'paper_config.json'
            first = run_paper(path,config,root/'state')
            again = run_paper(path,config,root/'state')
            self.assertEqual(first,again)
            self.assertEqual(first['orders'],5)
            (root/'state/journal.sqlite').unlink()
            with self.assertRaisesRegex(ValueError,'Incomplete'):
                run_paper(path,config,root/'state')

    def test_declared_data_outage_cannot_be_interpreted_as_cash_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = demo_snapshot()
            data['universe_complete'] = False
            path = root/'snapshot.json'
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                run_paper(path,Path(__file__).resolve().parents[1]/'paper_config.json',root/'state')
            self.assertFalse((root/'state').exists())


if __name__ == '__main__':
    unittest.main()
