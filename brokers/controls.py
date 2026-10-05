"""Broker-owned account checks, canonical state paths and execution sessions."""
from contextlib import closing, contextmanager
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sqlite3

from app.control_contracts import ControlError, project_path


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
        if connector.mode != self.mode or connector.mode not in {'paper', 'broker_paper'}:
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

    def session(self, cfg, snapshot, fees):
        from app.assembly import open_broker_paper_session
        connector = self.connector()
        self.validate_binding(connector)
        return open_broker_paper_session(dict(cfg, broker=self.connector_config()), snapshot, self.state_dir, fees)


class OpenDPaperControl(BrokerControl):
    def financial_account_reference(self):
        from brokers.opend_broker import _identity
        connector = self.connector()
        self.validate_binding(connector)
        cfg = connector._settings()
        return _identity(dict(provider=connector.provider, environment='SIMULATE',
                              user_id=str(cfg.login_user_id), account_id=str(cfg.account_id)))

    def financial_observation(self):
        from brokers.opend_observation import financial_observation
        from platforms.opend import OpenDError
        connector = self.connector()
        self.validate_binding(connector)
        try:
            return financial_observation(connector)
        except OpenDError:
            raise ControlError('账户估值读取失败；请检查 OpenD 登录与模拟账户绑定，历史记录已保留。') from None

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
            raise ControlError('模拟账户读取失败，请检查 OpenD 登录、账户绑定及未完成委托。') from None

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
            raise ControlError('券商行情读取失败，请检查 OpenD 登录及美股报价权限。') from None

    def validate_binding(self, connector):
        # The concrete connector fixes its provider and environment; never supplied by the UI.
        settings = connector._settings()
        if connector.environment != 'SIMULATE' or not settings.account_id or not settings.login_user_id:
            raise ControlError('请先在券商配置中明确绑定模拟账号与登录 ID。')


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
        return open_paper_session(dict(cfg, broker=self.connector_config()), snapshot, self.state_dir, fees)
