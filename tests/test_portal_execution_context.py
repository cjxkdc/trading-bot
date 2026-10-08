"""Executable-file inputs retain broker metadata and per-write ownership guards."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.control_contracts import ControlError, SnapshotInput
from app.portal_worker import perform, require_snapshot_context
from data.snapshot import demo_snapshot, parse_snapshot
from framework.decisions import Decision, digest
from framework.market import ExecutionContext, InstrumentRules
from framework.order_capabilities import US_EQUITY_LIMIT_ORDERS


class ExecutableSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.context = ExecutionContext('US', 'America/New_York', 'USD', 'equity',
                                        InstrumentRules(price_tick='0.001'))
        self.raw = demo_snapshot()
        snapshot = parse_snapshot(self.raw)
        self.loaded = SnapshotInput(snapshot.input_schema, False, self.raw, snapshot.stocks, {},
                                    {s.asset_id: {'ticker': s.ticker} for s in snapshot.stocks},
                                    snapshot, US_EQUITY_LIMIT_ORDERS)
        self.broker = {'execution_context': self.context.record(), 'mode': 'paper',
                       'capabilities': ['execute'], 'execution_enabled': True}

    def test_legacy_demo_keeps_cent_limits_on_finer_tick_account(self):
        require_snapshot_context(self.loaded, self.broker)
        self.assertIsNone(self.loaded.execution.execution_context)
        bad = dict(self.broker, execution_context=replace(self.context, currency='HKD').record())
        with self.assertRaises(ControlError):
            require_snapshot_context(self.loaded, bad)
        bad = dict(self.broker, execution_context=replace(self.context,
                    instrument_rules=InstrumentRules(contract_multiplier='100')).record())
        with self.assertRaises(ControlError):
            require_snapshot_context(self.loaded, bad)

    def test_explicit_context_must_match_account_and_declared_requirements(self):
        explicit = replace(self.loaded, execution=replace(self.loaded.execution, execution_context=self.context))
        require_snapshot_context(explicit, self.broker)
        for context in (replace(self.context, market='HK'),
                        replace(self.context, timezone='Asia/Hong_Kong'),
                        replace(self.context, instrument_rules=InstrumentRules(contract_multiplier='100'))):
            with self.subTest(context=context), self.assertRaises(ControlError):
                require_snapshot_context(replace(explicit, execution=replace(explicit.execution,
                    execution_context=context)), self.broker)
        with self.assertRaises(ControlError):
            require_snapshot_context(replace(explicit, order_requirements=replace(US_EQUITY_LIMIT_ORDERS,
                currency='HKD')), self.broker)

    def test_file_cannot_supply_unverified_instrument_override_or_quote_currency(self):
        explicit = replace(self.loaded, execution=replace(self.loaded.execution, execution_context=self.context))
        symbol, quote = next(iter(explicit.execution.quotes.items()))
        for changed in (replace(quote, rules=InstrumentRules(contract_multiplier='100')),
                        replace(quote, currency='HKD')):
            quotes = dict(explicit.execution.quotes, **{symbol: changed})
            with self.assertRaises(ControlError):
                require_snapshot_context(replace(explicit, execution=replace(explicit.execution, quotes=quotes)), self.broker)

    def test_per_symbol_lot_is_accepted_only_when_broker_resolver_confirms_it(self):
        from brokers.controls import BrokerControl, require_account_snapshot, snapshot_instruments
        market = ExecutionContext('HK', 'Asia/Hong_Kong', 'HKD', 'equity',
                                  InstrumentRules(quantity_step='100', min_quantity='100', price_tick='.05'))
        asset, quote = next(iter(self.loaded.execution.quotes.items()))
        lot = InstrumentRules(quantity_step='500', min_quantity='500', price_tick='.10')
        snapshot = replace(self.loaded.execution, execution_context=market,
                           quotes={asset: replace(quote, rules=lot, currency='HKD')},
                           instruments={asset: '00001'})
        loaded = replace(self.loaded, execution=snapshot, order_requirements=replace(
            US_EQUITY_LIMIT_ORDERS, market='HK', currency='HKD'))
        broker = dict(self.broker, execution_context=market.record())
        resolver = Mock(return_value={asset: lot})
        control = SimpleNamespace(instrument_rules=resolver)
        require_snapshot_context(loaded, broker, control)
        resolver.assert_called_once_with({asset: '00001'})
        with self.assertRaises(ControlError):
            require_snapshot_context(loaded, broker)
        resolver.return_value = {}
        with self.assertRaises(ControlError):
            require_snapshot_context(loaded, broker, control)
        resolver.return_value = {asset: market.instrument_rules}
        with self.assertRaises(ControlError):
            require_snapshot_context(loaded, broker, control)
        # The same validation occurs again at session entry, including frozen
        # preparations that did not originate in the executable-file workflow.
        with tempfile.TemporaryDirectory() as folder:
            account = BrokerControl(folder, {'state_dir': 'runs/account'},
                                    {'control': {'mode': 'broker_paper', 'execution_context': market.record()}})
            @contextmanager
            def opened(*args, **kwargs):
                yield SimpleNamespace(mode='broker_paper'), None
            with patch.object(account, 'connector'), patch.object(account, 'connector_config', return_value={}), \
                 patch.object(account, 'require_execution'), \
                 patch.object(account, 'instrument_rules', return_value={asset: lot}) as trusted, \
                 patch('app.assembly.open_account_session', opened):
                with account.session({}, snapshot, None) as (connection, _):
                    self.assertEqual(connection.mode, 'broker_paper')
                trusted.return_value = {asset: market.instrument_rules}
                with self.assertRaises(ControlError):
                    with account.session({}, snapshot, None):
                        self.fail('accepted stale frozen lot rules')

    def test_live_cli_session_rejects_conflicting_snapshot_before_opening_account(self):
        from brokers.controls import BrokerControl
        entry = {'control': {'mode': 'live', 'execution_context': self.context.record()}}
        with tempfile.TemporaryDirectory() as folder:
            control = BrokerControl(folder, {'state_dir': 'runs/account'}, entry)
            mismatched = replace(self.loaded.execution, execution_context=replace(self.context,
                instrument_rules=InstrumentRules(contract_multiplier='100')))
            with patch.object(control, 'connector'), \
                 patch.object(control, 'require_execution', return_value='a' * 64), \
                 patch('app.assembly.open_account_session') as open_session:
                with self.assertRaises(ControlError):
                    with control.session({}, mismatched, None):
                        self.fail('opened mismatched snapshot account')
            open_session.assert_not_called()
            self.assertFalse((Path(folder) / 'runs/account').exists())

    def test_direct_execution_checks_existing_ownership_guard_before_each_write(self):
        decision = Decision('decision', (), {'targets': []})
        runner = Mock(input_schema=self.loaded.schema)
        runner.engine.strategy.history_requirements = SimpleNamespace(price_bars=1, dollar_volume_bars=1)
        runner.policy.max_quote_age_seconds = 60
        runner.plan.return_value = decision
        wrote = []
        def execute(*args, before_submit, **kwargs):
            for ident in ('first', 'second'):
                before_submit()
                wrote.append(ident)
            return {'orders': 2}
        runner.execute.side_effect = execute
        @contextmanager
        def session(*args):
            yield SimpleNamespace(mode='paper'), Mock()
        control = SimpleNamespace(session=session, instrument_rules=lambda instruments: {
            asset: self.context.instrument_rules for asset in instruments})
        source = Mock()
        source.load.return_value = self.loaded
        registry = Mock()
        registry.configuration.return_value = {}
        registry.descriptor.side_effect = lambda kind, ident: self.broker if kind == 'broker' else {'capabilities': ['preview']}
        registry.control.side_effect = lambda kind, ident, **kwargs: control if kind == 'broker' else source
        registry.resolve.return_value = ('paper', {'control': {'order_capabilities': {
            k: [v] for k,v in US_EQUITY_LIMIT_ORDERS.record().items()}}})
        token = digest({'context': 'ctx', 'snapshot': self.raw, 'decision': decision.record})
        guard = Mock(side_effect=[None, None, ControlError('account is now owned by an instance')])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'runs').mkdir()
            path = root / 'runs/input.json'
            path.write_text(json.dumps(self.raw), encoding='utf-8')
            with patch('app.portal_worker.ModuleRegistry', return_value=registry), \
                 patch('app.portal_worker.context_for', return_value='ctx'), \
                 patch('app.portal_worker.build_portfolio_runner', return_value=runner), \
                 patch('app.strategy_instances.require_unmanaged', guard):
                with self.assertRaisesRegex(ControlError, 'account is now owned'):
                    perform('execute', root=root, broker_id='paper', source_id='snapshot_file',
                            snapshot_path=str(path), preview_token=token,
                            clock=datetime.fromisoformat(self.raw['execution_time']))
        self.assertEqual(wrote, ['first'])
        self.assertEqual(guard.call_count, 3)
        self.assertEqual(registry.require_execution.call_count, 3)


if __name__ == '__main__':
    unittest.main()
