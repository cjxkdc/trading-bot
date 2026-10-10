"""Broker-owned account checks, canonical state paths and execution sessions."""
from contextlib import closing, contextmanager
from collections.abc import Mapping
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sqlite3

from app.control_contracts import ControlError, project_path
from framework.market import ExecutionContext, InstrumentRules
from framework.order_capabilities import OrderRequirements


def snapshot_instruments(snapshot):
    """Resolve data identifiers to broker symbols without trusting file lot rules."""
    mapping = dict(snapshot.instruments or {s.asset_id: s.ticker for s in snapshot.stocks})
    if any(asset not in mapping for asset in snapshot.quotes):
        raise ControlError('报价证券缺少券商代码映射。')
    return {asset: mapping[asset] for asset in snapshot.quotes}


def require_account_snapshot(snapshot, context, requirements=None, *, trusted_rules=None):
    """Executable files cannot replace the selected account's market or lot rules."""
    try:
        expected = ExecutionContext.from_record(context)
        actual = snapshot.market
        if requirements is not None and (not isinstance(requirements, OrderRequirements) or
                any(getattr(requirements, key) != getattr(actual, key)
                    for key in ('market', 'asset_class', 'currency'))):
            raise ValueError('snapshot contradicts its order requirements')
        if snapshot.execution_context is None:
            # Original US snapshots used whole shares and cent limits. A module
            # with a finer price tick can honor those unchanged legacy orders.
            legacy = ExecutionContext.legacy_us()
            if (any(getattr(expected, key) != getattr(legacy, key)
                    for key in ('market', 'timezone', 'asset_class', 'currency'))
                    or any(getattr(expected.instrument_rules, key) != getattr(legacy.instrument_rules, key)
                           for key in ('quantity_step', 'min_quantity', 'contract_multiplier'))
                    or legacy.instrument_rules.price_tick % expected.instrument_rules.price_tick):
                raise ValueError('legacy snapshot is incompatible with this account')
        elif actual != expected:
            raise ValueError('snapshot execution context differs from account')
        if trusted_rules is None:
            trusted_rules = {asset: expected.instrument_rules for asset in snapshot.quotes}
        if not isinstance(trusted_rules, Mapping):
            raise ValueError('broker instrument rules are missing')
        for asset, quote in snapshot.quotes.items():
            if quote.currency is not None and quote.currency != expected.currency:
                raise ValueError('quote currency differs from account')
            # Broker-resolved metadata is authoritative. Frozen or imported
            # quote rules must agree; never silently replace approved quantities.
            verified = InstrumentRules.from_record(trusted_rules[asset])
            rules = actual.rules_for(asset, quote)
            if snapshot.execution_context is None:
                if (any(getattr(rules, key) != getattr(verified, key)
                        for key in ('quantity_step', 'min_quantity', 'contract_multiplier'))
                        or rules.price_tick % verified.price_tick):
                    raise ValueError('legacy quote rules differ from broker metadata')
            elif rules != verified:
                raise ValueError('unverified quote instrument rules')
            if requirements is not None and requirements.quantity_type == 'whole' and (rules.quantity_step % 1 or rules.min_quantity % 1):
                raise ValueError('whole-quantity requirement contradicts lot rules')
    except (KeyError, TypeError, AttributeError, ValueError, ArithmeticError):
        raise ControlError('执行快照的市场、币种或交易规则与所选账户不一致，请重新获取匹配的数据。') from None


class BrokerControl:
    def __init__(self, root, profile, entry):
        self.root, self.profile, self.entry = Path(root), dict(profile), entry
        self.mode = entry['control']['mode']
        self.state_dir = project_path(root, profile['state_dir'], under='runs')
        self.journal_path = self.state_dir / 'journal.sqlite'

    def config_revision(self):
        hasher = sha256()
        if 'config_dir' in self.profile:
            folder = project_path(self.root, self.profile['config_dir'])
            for path in sorted(folder.glob('*')):
                if path.is_file():
                    if path.stat().st_size > 1024 * 1024:
                        raise ControlError('账户连接配置文件超过读取上限。')
                    hasher.update(path.name.encode())
                    hasher.update(path.read_bytes())
        return hasher.hexdigest()

    def connector_config(self):
        paths = self.entry['control'].get('path_fields', [])
        return {key: str(project_path(self.root, value)) if key in paths else value
                for key, value in self.profile.items()}

    def connector(self):
        module, name = self.entry['entrypoint'].split(':')
        cfg = self.connector_config()
        connector = getattr(importlib.import_module(module), name)(**{k: v for k, v in cfg.items() if k != 'type'})
        if connector.mode != self.mode or connector.mode not in {'paper', 'broker_paper', 'live'}:
            raise ControlError('券商模块报告的账户环境与登记不一致。')
        return connector

    def validate_binding(self, connector):
        pass

    def inspect(self, *, connect=False):
        connector = self.connector()
        if connect:
            self.validate_binding(connector)
        result = connector.inspect(connect=connect)
        return result

    def execution_context(self):
        from framework.market import ExecutionContext
        raw = self.entry['control'].get('execution_context')
        if raw is None:
            raise ControlError('券商模块必须声明市场、时区、币种与交易单位。')
        try:
            return ExecutionContext.from_record(raw).record()
        except (ValueError, TypeError, ArithmeticError):
            raise ControlError('券商模块的市场、时区或交易单位配置无效。') from None

    def instrument_rules(self, instruments):
        """Return broker-verified rules keyed by asset ID (input maps IDs to symbols).

        Adapters with per-symbol lots/ticks override this resolver. The default
        is valid only for modules whose declared rules apply to every symbol.
        """
        rules = ExecutionContext.from_record(self.execution_context()).instrument_rules
        return {asset: rules for asset in instruments}

    def execution_account_reference(self):
        connector = self.connector()
        self.validate_binding(connector)
        reference = getattr(connector, 'execution_account_reference', None)
        if not callable(reference):
            raise ControlError('实盘模块必须提供独立验证的账户身份。')
        return reference()

    def require_execution(self):
        from app.execution_policy import load_execution_policy
        if 'execute' not in self.entry['control'].get('capabilities', []):
            raise ControlError('此账户模块只提供读取能力，未声明订单执行。')
        reference = self.execution_account_reference() if self.mode == 'live' else None
        load_execution_policy(self.root).require_execution(
            self.mode, getattr(self, 'instance_id', None), reference)
        return reference

    @contextmanager
    def session(self, cfg, snapshot, fees):
        from app.assembly import open_account_session
        connector = self.connector()
        self.validate_binding(connector)
        reference = self.require_execution()
        require_account_snapshot(snapshot, self.execution_context(),
                                 trusted_rules=self.instrument_rules(snapshot_instruments(snapshot)))
        def revalidate():
            if self.require_execution() != reference:
                raise ControlError('账户绑定已变化，原连接不能继续发送订单。')
        with open_account_session(dict(cfg, broker=self.connector_config()), snapshot,
                                  self.state_dir, fees, expected_mode=self.mode,
                                  enable_live=self.mode == 'live', expected_account_ref=reference) as (broker, journal):
            yield (ExecutionAuthorizedBroker(broker, revalidate) if self.mode == 'live' else broker), journal


class ExecutionAuthorizedBroker:
    """Keep an in-flight batch from outliving a server grant or account binding."""
    def __init__(self, broker, revalidate):
        self._broker, self._revalidate = broker, revalidate

    def __getattr__(self, name):
        return getattr(self._broker, name)

    def submit(self, intent):
        self._revalidate()
        return self._broker.submit(intent)

    def cancel(self, broker_id):
        self._revalidate()
        return self._broker.cancel(broker_id)

    def unlock(self, password):
        self._revalidate()
        return self._broker.unlock(password)


class OpenDControl(BrokerControl):
    expected_environment = None

    def public_settings(self):
        from brokers.opend_settings import public_settings
        return public_settings(self)

    def configure(self, values):
        from brokers.opend_settings import configure
        return configure(self, values)

    def financial_account_reference(self):
        return self.execution_account_reference()

    def financial_observation(self):
        from brokers.opend_observation import financial_observation, ObservationBlocked
        from platforms.opend import OpenDError
        connector = self.connector()
        self.validate_binding(connector)
        try:
            return financial_observation(connector)
        except ObservationBlocked as exc:
            # Unsupported holdings or currencies are explicit, static reasons;
            # they must not be reported as a login or connection problem.
            raise ControlError(str(exc)) from None
        except OpenDError:
            raise ControlError('账户估值读取失败；请检查 OpenD 登录与账户绑定，历史记录已保留。') from None

    def trading_sessions(self, start, end):
        from brokers.opend_observation import trading_sessions
        from platforms.opend import OpenDError
        connector = self.connector()
        self.validate_binding(connector)
        try:
            return trading_sessions(connector, start, end)
        except OpenDError:
            raise ControlError('交易日历读取失败，请检查 OpenD 连接；自动交易已暂停。') from None

    @contextmanager
    def reconciliation_session(self, fees, snapshot=None):
        """Query existing durable intents; never initialize state or allow writes at broker."""
        from framework.journal import Journal
        connector = self.connector()
        self.validate_binding(connector)
        if not all((self.state_dir / name).is_file() for name in (*connector.state_files, 'journal.sqlite')):
            raise ControlError('执行账本不存在或不完整，不能自动重建账户历史。')
        connection = connector.connect(self.state_dir, snapshot, fees, read_only=True)
        try:
            journal = Journal(self.journal_path)
            try:
                yield connection, journal
            finally:
                journal.close()
        finally:
            connection.close()

    def observe_account(self):
        from brokers.opend_observation import account_observation, ObservationBlocked
        from platforms.opend import OpenDError
        connector = self.connector()
        self.validate_binding(connector)
        try:
            return account_observation(connector)
        except ObservationBlocked as exc:
            raise ControlError(str(exc)) from None
        except OpenDError:
            raise ControlError('账户读取失败，请检查 OpenD 登录、账户绑定及未完成委托。') from None

    def observe_quotes(self, instruments, max_age):
        from brokers.opend_observation import quote_observation, ObservationBlocked
        from platforms.opend import OpenDError
        connector = self.connector()
        self.validate_binding(connector)
        try:
            return quote_observation(connector, instruments, max_age)
        except ObservationBlocked as exc:
            raise ControlError(str(exc)) from None
        except OpenDError:
            raise ControlError('券商行情读取失败，请检查 OpenD 登录及该市场报价权限。') from None

    def validate_binding(self, connector):
        # The concrete connector fixes its provider and environment; never supplied by the UI.
        settings = connector._settings()
        if (connector.environment != self.expected_environment
                or not settings.account_id or not settings.login_user_id):
            raise ControlError('请先在券商配置中明确绑定对应环境的账号与登录 ID。')


class OpenDPaperControl(OpenDControl):
    expected_environment = 'SIMULATE'


class OpenDLiveControl(OpenDControl):
    expected_environment = 'REAL'


class TigerPaperControl(BrokerControl):
    def validate_binding(self, connector):
        from brokers.tiger_paper import _configured_account
        _configured_account(connector.config_dir)


class LocalPaperControl(BrokerControl):
    def connector_config(self):
        return {key: value for key, value in self.profile.items() if key != 'state_dir'}

    def inspect(self, *, connect=False):
        connector = self.connector()
        path = self.state_dir / connector.state_files[0]
        if not path.exists():
            return {'status': 'ready', 'network_requested': False, 'issues': [], 'initialized': False,
                    'currency': 'USD', 'cash': None, 'positions': None,
                    'message': '本地回放账户尚未创建；首次明确执行时才初始化。'}
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            account = db.execute('SELECT cash FROM account').fetchall()
            if len(account) != 1:
                raise ControlError('本地账户状态无法核对，请保留原账本。')
            positions = [{'symbol': s, 'quantity': q} for s, q in db.execute('SELECT symbol,quantity FROM positions')]
            orders = [json.loads(s) for (s,) in db.execute('SELECT state FROM orders')]
        return {'status': 'connected', 'issues': [], 'initialized': True, 'network_requested': False,
                'currency': 'USD', 'cash': account[0][0], 'positions': positions,
                'open_order_count': sum(s['status'] not in {'filled', 'cancelled', 'rejected'} for s in orders)}

    def session(self, cfg, snapshot, fees):
        from app.assembly import open_paper_session
        self.connector()
        self.require_execution()
        require_account_snapshot(snapshot, self.execution_context(),
                                 trusted_rules=self.instrument_rules(snapshot_instruments(snapshot)))
        return open_paper_session(dict(cfg, broker=self.connector_config()), snapshot, self.state_dir, fees)
