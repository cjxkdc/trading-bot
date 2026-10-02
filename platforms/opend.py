"""Shared OpenD transport; Futu HK and moomoo AU remain explicit profiles."""
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from hashlib import md5
import json
from pathlib import Path
import socket

ROOT = Path(__file__).resolve().parents[1]
SDK_VERSION = '10.11.7108'
PROFILES = {
    'futu': {'package': 'futu-api', 'firm': 'FUTUSECURITIES', 'port': 11111},
    'moomoo': {'package': 'moomoo-api', 'firm': 'FUTUAU', 'port': 11112},
}


class OpenDError(RuntimeError):
    """Sanitized adapter failure; never expose SDK errors, credentials or raw IDs."""


@dataclass(frozen=True)
class Settings:
    provider: str
    host: str
    port: int
    account_id: str | None
    login_user_id: str | None
    environment: str = 'SIMULATE'


def settings(provider, config_dir=None, *, environment='SIMULATE'):
    if provider not in PROFILES or environment not in {'SIMULATE', 'REAL'}:
        raise OpenDError('Unknown OpenD provider or trading environment')
    suffix = '_live' if environment == 'REAL' else ''
    folder = ROOT / (config_dir or ('secrets/' + provider + suffix))
    path = folder / 'connection.json'
    value = {}
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            raise OpenDError('Unable to read connection.json') from None
    if not isinstance(value, dict) or set(value) - {'host', 'port', 'account_id', 'login_user_id'}:
        raise OpenDError('Unsupported OpenD connection settings')
    host, port = value.get('host', '127.0.0.1'), value.get('port', PROFILES[provider]['port'])
    if host != '127.0.0.1' or type(port) is not int or not 1 <= port <= 65535:
        raise OpenDError('OpenD must use IPv4 loopback and a valid local port')
    ids = []
    for key in ('account_id', 'login_user_id'):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or not item.isascii()
                                 or not item.isdigit() or int(item) <= 0):
            raise OpenDError('Account and login IDs must be positive integer strings or null')
        ids.append(item)
    return Settings(provider, host, port, *ids, environment)


def configuration_check(provider, config_dir=None, *, environment='SIMULATE'):
    cfg = settings(provider, config_dir, environment=environment)
    try:
        sdk = version(PROFILES[provider]['package'])
    except PackageNotFoundError:
        sdk = None
    issues = [] if sdk == SDK_VERSION else ['Install requirements-' + provider + '.txt']
    if environment == 'REAL' and (cfg.account_id is None or cfg.login_user_id is None):
        issues.append('Live connections require explicit account_id and login_user_id in connection.json')
    return {'provider': provider, 'mode': 'offline_check', 'status': 'not_ready' if issues else 'configuration_ready_not_authenticated',
            'network_requested': False, 'host': cfg.host, 'port': cfg.port,
            'configured_firm': PROFILES[provider]['firm'], 'sdk_version': sdk,
            'account_selected': cfg.account_id is not None, 'account_environment': environment, 'issues': issues}


def load_sdk(provider):
    if provider not in PROFILES:
        raise OpenDError('Unknown OpenD provider')
    try:
        if version(PROFILES[provider]['package']) != SDK_VERSION:
            raise OpenDError('Unsupported OpenD SDK version')
        sdk = import_module(provider)
        # Both SDKs share logger names. Disable their raw protocol/account logging.
        log = import_module(provider + '.common.ft_logger').logger
        log.file_logger.disabled = log.console_logger.disabled = True
        sdk.SysConfig.set_all_thread_daemon(True)
        return sdk
    except OpenDError:
        raise
    except (Exception, SystemExit):
        raise OpenDError('Unable to load the pinned OpenD SDK; check its dependencies') from None


def records(data):
    if not hasattr(data, 'to_dict'):
        raise OpenDError('Expected an OpenD data table')
    rows = data.to_dict(orient='records')
    if not isinstance(rows, list):
        raise OpenDError('Invalid OpenD data table')
    return rows


def call(context, method, **kwargs):
    try:
        result = getattr(context, method)(**kwargs)
        if not isinstance(result, tuple) or len(result) < 2 or result[0] != 0:
            raise OpenDError('OpenD request failed: ' + method + '; check login, permissions and supported account type')
        return result[1] if len(result) == 2 else result[1:]
    except OpenDError:
        raise
    except Exception:
        raise OpenDError('OpenD request failed: ' + method + '; reconcile any pending transmission before retry') from None


class BoundedConnection:
    def _init_connect_sync(self):
        # SDK constructors otherwise retry forever while OpenD is logged out.
        self._auto_reconnect = False
        self._sync_query_connect_timeout = 5
        return super()._init_connect_sync()

    def on_api_socket_reconnected(self):
        # Poll explicitly. Do not auto-unlock or subscribe every real account.
        return 0, ''


def trade_context_class(sdk, environment='SIMULATE'):
    if environment not in {'SIMULATE', 'REAL'}:
        raise OpenDError('Unknown trading environment')
    queries = import_module(sdk.__name__ + '.trade.trade_query')
    readers = {queries.AccInfoQuery.pack_req, queries.PositionListQuery.pack_req,
               queries.OrderListQuery.pack_req, queries.HistoryOrderListQuery.pack_req}
    writers = {queries.PlaceOrder.pack_req, queries.ModifyOrder.pack_req}

    class ScopedContext(BoundedConnection, sdk.OpenSecTradeContext):
        _verified_account = None
        _trading_environment = environment
        _verified_firm = None
        _allow_orders = False

        def _check_trd_env(self, trd_env):
            if trd_env != environment:
                raise OpenDError('Request environment does not match this adapter: ' + environment)
            return super()._check_trd_env(trd_env)

        def _check_acc_id_and_acc_index(self, trd_env, acc_id, acc_index):
            if not self._verified_account or str(acc_id) != self._verified_account:
                raise OpenDError('An explicit verified account ID is required')
            return super()._check_acc_id_and_acc_index(trd_env, acc_id, acc_index)

        def unlock_trade(self, password=None, password_md5=None, is_unlock=True):
            if (environment != 'REAL' or not self._allow_orders or not self._verified_account
                    or not self._verified_firm or not isinstance(password, str) or not password
                    or password_md5 is not None or is_unlock is not True):
                raise OpenDError('Explicit live execution and an in-memory trade password are required to unlock')
            # OpenD requires MD5 on the wire. Do not use the SDK helper: it retains the
            # password for reconnects and subscribes every account after unlocking.
            processor = self._get_sync_query_processor(queries.UnlockTrade.pack_req, queries.UnlockTrade.unpack_rsp)
            ret, _, _ = processor(is_unlock=True, password_md5=md5(password.encode('utf-8')).hexdigest(),
                                  conn_id=self.get_sync_conn_id(), security_firm=self._verified_firm)
            return (0, None) if ret == 0 else (ret, 'Trade unlock failed')

        def _get_sync_query_processor(self, pack_func, unpack_func):
            unlock = pack_func == queries.UnlockTrade.pack_req
            if unlock and environment != 'REAL':
                raise OpenDError('Simulation connections cannot unlock real trading')
            if pack_func != queries.GetAccountList.pack_req and pack_func not in readers | writers and not unlock:
                raise OpenDError('OpenD trade operation is not allowlisted')
            processor = super()._get_sync_query_processor(pack_func, unpack_func)

            def guarded(**kwargs):
                if unlock:
                    if (not self._allow_orders or not self._verified_account or not self._verified_firm
                            or kwargs.get('is_unlock') is not True
                            or kwargs.get('security_firm') != self._verified_firm):
                        raise OpenDError('Trade unlock is outside the explicitly enabled live connection')
                elif pack_func != queries.GetAccountList.pack_req:
                    if (kwargs.get('trd_env') != environment or not self._verified_account
                            or str(kwargs.get('acc_id')) != self._verified_account):
                        raise OpenDError('Request is outside the verified account and trading environment')
                    if pack_func in writers and not self._allow_orders:
                        raise OpenDError('Read-only diagnostics cannot place or cancel orders')
                    if pack_func == queries.ModifyOrder.pack_req and kwargs.get('modify_order_op') != sdk.ModifyOrderOp.CANCEL:
                        raise OpenDError('Only cancellation is supported; order modification is disabled')
                    if pack_func == queries.PlaceOrder.pack_req and (
                            kwargs.get('trd_mkt') != sdk.TrdMarket.US
                            or kwargs.get('order_type') != sdk.OrderType.NORMAL
                            or kwargs.get('time_in_force') != sdk.TimeInForce.DAY
                            or kwargs.get('fill_outside_rth') is not False
                            or kwargs.get('session') != sdk.Session.RTH):
                        raise OpenDError('Only regular-session US DAY limit orders are supported')
                return processor(**kwargs)
            return guarded
    return ScopedContext


def _check_identity(context, cfg):
    user_id = str(context.get_login_user_id())
    if not user_id.isdigit() or int(user_id) <= 0:
        raise OpenDError('Log in to the selected OpenD application first')
    if cfg.login_user_id is not None and cfg.login_user_id != user_id:
        raise OpenDError('OpenD is logged in as a different platform user')
    return user_id


def select_account(rows, cfg, sdk):
    if cfg.environment == 'REAL':
        if cfg.account_id is None or cfg.login_user_id is None:
            raise OpenDError('Live connections require explicit account and login IDs')
        eligible = [r for r in rows if str(r.get('acc_id')) == cfg.account_id
                    and r.get('trd_env') == sdk.TrdEnv.REAL
                    and r.get('acc_status') == sdk.TrdAccStatus.ACTIVE
                    and r.get('acc_type') in {sdk.TrdAccType.CASH, sdk.TrdAccType.MARGIN}
                    and r.get('acc_role') == sdk.TrdAccRole.NORMAL
                    and r.get('security_firm') == PROFILES[cfg.provider]['firm']
                    and sdk.TrdMarket.US in (r.get('trdmarket_auth') or [])]
        if len(eligible) != 1:
            raise OpenDError('Selected live account is not an active US-enabled normal account at the configured regional broker')
        return cfg.account_id, eligible[0]['security_firm']
    eligible = [r for r in rows if r.get('trd_env') == sdk.TrdEnv.SIMULATE
                and r.get('sim_acc_type') == sdk.SimAccType.STOCK
                and r.get('acc_status') == sdk.TrdAccStatus.ACTIVE
                and sdk.TrdMarket.US in r.get('trdmarket_auth', [])]
    if cfg.account_id is not None:
        eligible = [r for r in eligible if str(r.get('acc_id')) == cfg.account_id]
    if len(eligible) != 1:
        raise OpenDError('Select one active US STOCK simulation account in connection.json; real, option and competition accounts are excluded')
    row = eligible[0]
    account = str(row['acc_id'])
    if not account.isdigit() or int(account) <= 0:
        raise OpenDError('Invalid simulation account ID')
    # Classic paper accounts may have no regional broker affiliation.
    if row.get('security_firm') not in {None, '', 'N/A', PROFILES[cfg.provider]['firm']}:
        raise OpenDError('Unexpected regional broker on the selected account')
    return account, row.get('security_firm')


@contextmanager
def connection(cfg, *, trade=False, allow_orders=False, enable_live=False):
    if cfg.environment not in {'SIMULATE', 'REAL'}:
        raise OpenDError('Unknown trading environment')
    if cfg.environment == 'REAL':
        if cfg.account_id is None or cfg.login_user_id is None:
            raise OpenDError('Live connections require explicit account and login IDs before connecting')
        if allow_orders and enable_live is not True:
            raise OpenDError('Live order transmission must be explicitly enabled')
    sdk, context = load_sdk(cfg.provider), None
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=2):
            pass
        if trade:
            cls = trade_context_class(sdk, cfg.environment)
            context = cls(filter_trdmarket=sdk.TrdMarket.US, host=cfg.host, port=cfg.port,
                          security_firm=getattr(sdk.SecurityFirm, PROFILES[cfg.provider]['firm']))
        else:
            class QuoteContext(BoundedConnection, sdk.OpenQuoteContext):
                pass
            context = QuoteContext(host=cfg.host, port=cfg.port,
                                   security_firm=getattr(sdk.SecurityFirm, PROFILES[cfg.provider]['firm']))
        user_id = _check_identity(context, cfg)
        profile = {'user_id': user_id, 'provider': cfg.provider, 'environment': cfg.environment}
        if trade:
            account, firm = select_account(records(call(context, 'get_acc_list')), cfg, sdk)
            context._verified_account, context._allow_orders = account, allow_orders
            context._verified_firm = PROFILES[cfg.provider]['firm']
            profile.update(account_id=account, reported_firm=firm)
        yield sdk, context, profile
    except OpenDError:
        raise
    except Exception:
        raise OpenDError('OpenD connection failed; start and log in to the selected local gateway') from None
    finally:
        if context is not None:
            context.close()
