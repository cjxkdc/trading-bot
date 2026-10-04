"""Tiger-hosted paper trading: USD stocks, whole shares, DAY limit orders only."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import sqlite3

from framework.contracts import OrderState
from framework.journal import packed, unpack_order, unpack_state
from framework.trading import Account, Fees, amount
from platforms.tiger import CONFIG_FILE, GATEWAYS, configuration_check, _masked

ROOT = Path(__file__).resolve().parents[1]
METHODS = frozenset({'accounts', 'prime_assets', 'positions', 'orders', 'place_order', 'cancel_order'})
TERMINAL = {'filled', 'cancelled', 'rejected'}


class TigerPaperError(RuntimeError):
    """Only fixed, sanitized messages may cross the adapter boundary."""


def _value(value):
    return value.value if isinstance(value, Enum) else value


def _paper_format(account):
    return isinstance(account, str) and re.fullmatch(r'[0-9]{17}', account) is not None


def _configured_account(config_dir):
    check = configuration_check(config_dir)
    if check['issues']:
        raise TigerPaperError('Tiger paper configuration is not ready; run broker-status')
    from jproperties import Properties
    properties = Properties()
    with (Path(config_dir) / CONFIG_FILE).open('rb') as handle:
        properties.load(handle, 'utf-8')
    account = properties['account'].data.strip()
    if not _paper_format(account):
        raise TigerPaperError('tiger_paper rejects real accounts; export the 17-digit paper account')
    return account


def _api(client, method, **kwargs):
    try:
        return getattr(client, method)(**kwargs)
    except TigerPaperError:
        raise
    except Exception:
        raise TigerPaperError('Tiger paper API request failed; no automatic submission retry') from None


def _paper_client_class():
    from tigeropen.trade.trade_client import TradeClient

    class PaperOnlyClient(TradeClient):
        def execute(self, request, url=None):
            account = getattr(self, '_paper_account', None)
            method = getattr(request, '_method', None)
            model = getattr(request, 'biz_model', None)
            if (not _paper_format(account) or method not in METHODS
                    or str(getattr(model, 'account', '')) != account
                    or (url is not None and url not in GATEWAYS)):
                raise TigerPaperError('Request outside the selected Tiger paper account was blocked')
            if method != 'accounts' and not getattr(self, '_paper_verified', False):
                raise TigerPaperError('Tiger must confirm account_type PAPER before other requests')
            return super().execute(request, url=url)
    return PaperOnlyClient


def _connect_paper(config_dir):
    account = _configured_account(config_dir)  # Reject real IDs before client/network construction.
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    config = TigerOpenClientConfig(props_path=str(Path(config_dir).resolve()),
                                   sandbox_debug=False, enable_dynamic_domain=False)
    if config.server_url not in GATEWAYS or str(config.account) != account or not config.is_paper:
        raise TigerPaperError('Unexpected Tiger paper account or gateway')
    config.timeout, config.retry_max_tries, config.retry_max_time = 10, 1, 10
    config.token_refresh_duration, config.log_path = 0, None
    logger = logging.Logger('trading_bot_tiger_paper')
    logger.addHandler(logging.NullHandler())
    logger.propagate, logger.disabled = False, True
    client = _paper_client_class()(config, logger=logger)
    client._paper_account, client._paper_verified = account, False
    profiles = _api(client, 'get_managed_accounts', account=account)
    if (not profiles or len(profiles) != 1 or str(profiles[0].account) != account
            or _value(getattr(profiles[0], 'account_type', None)) != 'PAPER'):
        raise TigerPaperError('Tiger did not confirm this account as PAPER; connection blocked')
    client._paper_verified = True
    return client, account


def _orders(client, account):
    """Paginate all states; absence from a truncated page never proves no open order."""
    token, seen_tokens, rows, ids = '', set(), [], set()
    for _ in range(20):
        response = _api(client, 'get_orders', account=account, sec_type=None, limit=300, page_token=token)
        if response is None or not isinstance(getattr(response, 'result', None), list):
            raise TigerPaperError('Incomplete Tiger paper order list')
        for row in response.result:
            if str(row.account) != account or not getattr(row, 'id', None):
                raise TigerPaperError('Unexpected account or order identifier in Tiger response')
            if str(row.id) not in ids:
                ids.add(str(row.id))
                rows.append(row)
        next_token = getattr(response, 'next_page_token', None)
        if not next_token:
            # A full page without a continuation token is ambiguous, so fail closed.
            if len(response.result) >= 300:
                raise TigerPaperError('Tiger order pagination is ambiguous; execution blocked')
            return rows
        if next_token in seen_tokens:
            raise TigerPaperError('Tiger order pagination did not advance')
        seen_tokens.add(next_token)
        token = next_token
    raise TigerPaperError('Tiger order history exceeds the bounded scan; execution blocked')


def _status(row):
    status = str(_value(row.status)).replace('_', '').lower()
    return {'pendingnew': 'pending', 'initial': 'pending', 'new': 'pending',
            'submitted': 'accepted', 'held': 'accepted', 'partiallyfilled': 'partial',
            'filled': 'filled', 'cancelled': 'cancelled', 'canceled': 'cancelled',
            'pendingcancel': 'pending', 'inactive': 'rejected', 'rejected': 'rejected',
            'invalid': 'rejected', 'expired': 'rejected'}.get(status, 'unknown')


def _account(client, account, clock):
    started = clock()
    assets = _api(client, 'get_prime_assets', account=account, base_currency='USD')
    positions = _api(client, 'get_positions', account=account, sec_type=None)
    if assets is None or str(assets.account) != account or positions is None:
        raise TigerPaperError('Incomplete Tiger paper assets/positions response')
    segment = assets.segments.get('S')
    usd = segment.currency_assets.get('USD') if segment else None
    if usd is None:
        raise TigerPaperError('Tiger paper account has no USD cash balance')
    # USD cash only. Aggregated base-currency cash and margin buying power are not cash.
    try:
        cash = min(amount(usd.cash_balance), amount(usd.cash_available_for_trade))
        holdings = {}
        for row in positions:
            c = row.contract
            if (str(row.account) != account or _value(c.sec_type) != 'STK'
                    or _value(c.currency) != 'USD'):
                raise TigerPaperError('Execution requires an account containing only USD stocks')
            qty = amount(row.quantity)
            if qty != qty.to_integral_value() or c.symbol in holdings:
                raise TigerPaperError('Only unique, long, whole-share positions are supported')
            if qty:
                holdings[c.symbol] = qty
    except (ValueError, TypeError, ArithmeticError):
        raise TigerPaperError('Invalid or unavailable Tiger paper cash/position amounts') from None
    identity = 'tiger-paper:' + sha256(account.encode()).hexdigest()[:20]
    return Account(identity, 'USD', cash, tuple(sorted(holdings.items())), started)


class TigerPaperConnector:
    mode = 'broker_paper'
    state_files = ('tiger-paper.sqlite',)

    def __init__(self, config_dir='secrets/tiger_paper', state_dir='runs/tiger-paper'):
        self.config_dir = (ROOT / config_dir).resolve()
        self.state_dir = (ROOT / state_dir).resolve()

    def inspect(self, *, connect=False):
        check = configuration_check(self.config_dir)
        check.update(broker='tiger_paper', account_environment='tiger_paper', mode='read_only')
        if not check['issues']:
            try:
                _configured_account(self.config_dir)
            except TigerPaperError as exc:
                check['issues'].append(str(exc))
                check['status'] = 'not_ready'
        if not connect or check['issues']:
            return check
        client, account = _connect_paper(self.config_dir)
        value = _account(client, account, lambda: datetime.now(timezone.utc))
        active = [r for r in _orders(client, account) if _status(r) not in TERMINAL]
        return dict(check, status='connected', network_requested=True, account=_masked(account),
                    account_type='PAPER', currency=value.currency, cash=str(value.cash),
                    positions=[{'symbol': s, 'quantity': str(q)} for s, q in value.positions],
                    open_order_count=len(active))

    def _path(self, state_dir):
        if Path(state_dir).resolve() != self.state_dir:
            raise TigerPaperError('Use the configured Tiger paper state directory; do not create a second ledger')
        return self.state_dir / self.state_files[0]

    def initialize(self, state_dir, initial_cash):
        account = _configured_account(self.config_dir)
        path = self._path(state_dir)
        # initial_cash never creates or resets funds at Tiger.
        with path.open('xb'):
            pass
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE binding (account_hash TEXT PRIMARY KEY);
                CREATE TABLE orders (client_id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    broker_id TEXT UNIQUE, state TEXT);
            ''')
            db.execute('INSERT INTO binding VALUES (?)', (sha256(account.encode()).hexdigest(),))
            db.commit()

    def connect(self, state_dir, snapshot, fees):
        path = self._path(state_dir)
        client, account = _connect_paper(self.config_dir)
        symbols = None if snapshot is None else {s.asset_id: s.ticker for s in snapshot.stocks}
        return TigerPaperBroker(client, account, path, fees, symbol_map=symbols)


class TigerPaperBroker:
    mode = 'broker_paper'

    def __init__(self, client, account, path, fees=None, clock=None, symbol_map=None):
        if (not _paper_format(account) or not getattr(client, '_paper_verified', False)
                or getattr(client, '_paper_account', None) != account):
            raise TigerPaperError('Verified Tiger PAPER account required')
        self.client, self.account_id = client, account
        self.fees = fees or Fees()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.symbol_map = None if symbol_map is None else dict(symbol_map)
        if self.symbol_map is not None and len(set(self.symbol_map.values())) != len(self.symbol_map):
            raise TigerPaperError('Ambiguous asset ID to Tiger ticker mapping')
        path = Path(path).resolve()
        if not path.is_file():
            raise TigerPaperError('Tiger paper order ledger is missing')
        self.db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        try:
            if self.db.execute('SELECT account_hash FROM binding').fetchall() != [(sha256(account.encode()).hexdigest(),)]:
                raise TigerPaperError('Tiger paper ledger belongs to a different account')
            self.db.execute('SELECT client_id,body,broker_id,state FROM orders LIMIT 0')
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise TigerPaperError('Tiger paper ledger integrity check failed')
        except Exception:
            self.db.close()
            raise TigerPaperError('Invalid Tiger paper ledger; never recreate lost order state') from None

    def close(self):
        self.db.close()

    def execution_time(self):
        return self.clock()

    def account(self):
        account = _account(self.client, self.account_id, self.clock)
        if self.symbol_map is None:
            return account
        reverse = {ticker: asset_id for asset_id, ticker in self.symbol_map.items()}
        if any(ticker not in reverse for ticker, _ in account.positions):
            raise TigerPaperError('Snapshot must include asset mappings for every held Tiger stock')
        return Account(account.account_id, account.currency, account.cash,
                       tuple(sorted((reverse[t], q) for t, q in account.positions)), account.as_of)

    def _ticker(self, asset_id):
        if self.symbol_map is None:
            return asset_id
        if asset_id not in self.symbol_map:
            raise TigerPaperError('Missing Tiger ticker mapping for asset ID')
        return self.symbol_map[asset_id]

    @staticmethod
    def _decode(body):
        value = json.loads(body)
        # Initial connection-test entries used ticker IDs directly.
        ticker = value.pop('broker_symbol', value['symbol'])
        return unpack_order(packed(value)), ticker

    def open_orders(self):
        remote_ids = {str(r.id) for r in _orders(self.client, self.account_id) if _status(r) not in TERMINAL}
        # An absent server order does not settle a timed-out local transmission.
        for client_id, body in self.db.execute('SELECT client_id,state FROM orders').fetchall():
            if body is None or unpack_state(body).status not in TERMINAL:
                state = self.query(client_id)
                if state.status not in TERMINAL:
                    remote_ids.add(state.broker_id or 'unknown:' + client_id)
        return tuple(sorted(remote_ids))

    @staticmethod
    def _tag(client_id):
        return 'tb' + sha256(client_id.encode()).hexdigest()[:30]

    def _save(self, intent, ticker, remote):
        c = remote.contract
        if (str(remote.account) != self.account_id or _value(c.sec_type) != 'STK'
                or _value(c.currency) != 'USD' or c.symbol != ticker
                or _value(remote.action) != intent.side or _value(remote.order_type) != 'LMT'
                or amount(remote.quantity) != intent.quantity
                or amount(remote.limit_price) != intent.limit_price
                or getattr(remote, 'quantity_scale', 0) not in (None, 0)
                or getattr(remote, 'filled_scale', 0) not in (None, 0)
                or getattr(remote, 'user_mark', None) != self._tag(intent.client_id)):
            raise TigerPaperError('Tiger order identity differs from the saved intent')
        fill = amount(remote.filled)
        average = amount(remote.avg_fill_price or 0)
        state = OrderState(intent.client_id, str(remote.id), _status(remote), fill,
                           average, amount(remote.commission or 0))
        previous = self.db.execute('SELECT broker_id,state FROM orders WHERE client_id=?', (intent.client_id,)).fetchone()
        if (not remote.id or fill > intent.quantity or fill != fill.to_integral_value()
                or (state.status == 'filled' and fill != intent.quantity)
                or (fill and (average <= 0 or (intent.side == 'BUY' and average > intent.limit_price)
                             or (intent.side == 'SELL' and average < intent.limit_price)))
                or (previous[0] and previous[0] != state.broker_id)
                or (previous[1] and unpack_state(previous[1]).filled_quantity > fill)):
            raise TigerPaperError('Inconsistent Tiger paper fill or broker identifier')
        with self.db:
            self.db.execute('UPDATE orders SET broker_id=?,state=? WHERE client_id=?',
                            (state.broker_id, packed(asdict(state)), intent.client_id))
        return state

    def query(self, client_id):
        record = self.db.execute('SELECT body,broker_id FROM orders WHERE client_id=?', (client_id,)).fetchone()
        if record is None:
            return OrderState(client_id, None, 'unknown', Decimal(0))
        intent, ticker = self._decode(record[0])
        broker_id = record[1]
        if broker_id:
            remote = _api(self.client, 'get_order', account=self.account_id, id=int(broker_id))
        else:
            matches = [r for r in _orders(self.client, self.account_id)
                       if getattr(r, 'user_mark', None) == self._tag(client_id)]
            if len(matches) > 1:
                raise TigerPaperError('Multiple Tiger orders match one local intent; reconciliation required')
            remote = matches[0] if matches else None
        if remote is None:
            return OrderState(client_id, broker_id, 'unknown', Decimal(0))
        return self._save(intent, ticker, remote)

    def submit(self, intent):
        ticker = self._ticker(intent.symbol)
        body = packed(dict(asdict(intent), broker_symbol=ticker))
        prior = self.db.execute('SELECT body FROM orders WHERE client_id=?', (intent.client_id,)).fetchone()
        if prior:
            if self._decode(prior[0]) != (intent, ticker):
                raise TigerPaperError('Client ID was reused for a different order')
            return self.query(intent.client_id)
        if (not intent.client_id or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', ticker)
                or ticker.startswith('DEMO') or intent.side not in {'BUY', 'SELL'}
                or amount(intent.quantity, True) != intent.quantity.to_integral_value()
                or amount(intent.limit_price, True) * intent.quantity > 100000):
            raise TigerPaperError('Only USD stock whole-share limit orders up to USD 100000 are supported')
        if self.open_orders():
            raise TigerPaperError('Tiger paper account has unresolved orders')
        value = self.account()
        if intent.side == 'BUY' and intent.quantity * intent.limit_price + self.fees.estimate(
                intent.quantity, intent.limit_price) > value.cash:
            raise TigerPaperError('Insufficient USD paper cash including estimated fees')
        if intent.side == 'SELL' and intent.quantity > dict(value.positions).get(intent.symbol, Decimal(0)):
            raise TigerPaperError('Cannot sell unowned paper shares')
        from tigeropen.common.util.contract_utils import stock_contract
        from tigeropen.common.util.order_utils import limit_order
        order = limit_order(self.account_id, stock_contract(ticker, 'USD'), intent.side,
                            int(intent.quantity), float(intent.limit_price), time_in_force='DAY')
        order.outside_rth, order.user_mark = False, self._tag(intent.client_id)
        # Claim is committed before network I/O. A timeout never permits resubmission.
        with self.db:
            self.db.execute('INSERT INTO orders VALUES (?,?,NULL,NULL)', (intent.client_id, body))
        broker_id = _api(self.client, 'place_order', order=order)
        if broker_id is not None:
            if not str(broker_id).isdigit() or int(broker_id) <= 0:
                raise TigerPaperError('Tiger did not return a valid order ID; reconcile before continuing')
            with self.db:
                self.db.execute('UPDATE orders SET broker_id=? WHERE client_id=?', (str(broker_id), intent.client_id))
        return self.query(intent.client_id)

    def cancel(self, broker_id):
        record = self.db.execute('SELECT client_id FROM orders WHERE broker_id=?', (str(broker_id),)).fetchone()
        if record is None:
            raise TigerPaperError('Cancellation is limited to orders owned by this paper ledger')
        state = self.query(record[0])
        if state.status in TERMINAL:
            return state
        if state.status == 'unknown':
            raise TigerPaperError('Cannot cancel an unverified paper order')
        _api(self.client, 'cancel_order', account=self.account_id, id=int(broker_id))
        return self.query(record[0])  # The cancel acknowledgement is not a final fill state.
