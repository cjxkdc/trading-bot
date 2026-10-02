"""Shared US stock order lifecycle; paper and live profiles fix their own environment."""
from contextlib import closing, contextmanager, ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

from framework.contracts import OrderState
from framework.journal import packed, unpack_order, unpack_state
from framework.trading import Account, Fees, amount
from platforms.opend import (ROOT, OpenDError, call, configuration_check,
                             connection, records, settings)

TERMINAL = {'filled', 'cancelled', 'rejected'}
STATUSES = {
    'UNSUBMITTED': 'pending', 'WAITING_SUBMIT': 'pending', 'SUBMITTING': 'pending',
    'SUBMITTED': 'accepted', 'FILLED_PART': 'partial', 'FILLED_ALL': 'filled',
    'CANCELLING_PART': 'pending', 'CANCELLING_ALL': 'pending',
    'CANCELLED_PART': 'cancelled', 'CANCELLED_ALL': 'cancelled', 'DELETED': 'cancelled',
    'SUBMIT_FAILED': 'rejected', 'FAILED': 'rejected', 'DISABLED': 'rejected',
}


def _number(value, positive=False):
    try:
        return amount(value, positive)
    except (TypeError, ValueError, ArithmeticError):
        raise OpenDError('Missing or invalid OpenD cash, position or order amount') from None


def _code(ticker):
    if (not isinstance(ticker, str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', ticker)
            or ticker.startswith('DEMO')):
        raise OpenDError('Only US stock tickers are supported; synthetic symbols are excluded')
    return 'US.' + ticker


def _identity(profile):
    parts = [profile['provider'], profile['user_id'], profile['account_id']]
    # Preserve existing paper fingerprints/ledgers; live identity is disjoint.
    if profile.get('environment', 'SIMULATE') == 'REAL':
        parts.insert(1, 'REAL')
    return sha256(packed(parts).encode()).hexdigest()


def _scope(sdk, profile):
    environment = profile.get('environment', 'SIMULATE')
    if environment not in {'SIMULATE', 'REAL'}:
        raise OpenDError('Unknown account environment')
    return {'trd_env': getattr(sdk.TrdEnv, environment), 'acc_id': int(profile['account_id'])}


def _account(sdk, client, profile, clock, instrument_check=None):
    started = clock()
    scope = _scope(sdk, profile)
    assets = records(call(client, 'accinfo_query', **scope, currency=sdk.Currency.USD, refresh_cache=True))
    rows = records(call(client, 'position_list_query', **scope, refresh_cache=True,
                        position_market=sdk.TrdMarket.NONE, currency=sdk.Currency.USD))
    if len(assets) != 1:
        raise OpenDError('Expected exactly one account asset record')
    # Aggregated cash and margin buying power can include other currencies/credit.
    cash = min(_number(assets[0].get('us_cash')), _number(assets[0].get('usd_net_cash_power')))
    holdings, sellable = {}, {}
    for row in rows:
        code = row.get('code', '')
        if (not code.startswith('US.') or _code(code[3:]) != code
                or row.get('position_side') != sdk.PositionSide.LONG
                or row.get('currency') != sdk.Currency.USD
                or str(row.get('acc_id', profile['account_id'])) not in {profile['account_id'], 'N/A', 'None'}):
            raise OpenDError('Account must contain only long USD stock positions')
        qty, available = _number(row.get('qty')), _number(row.get('can_sell_qty'))
        if qty != qty.to_integral_value() or available != available.to_integral_value() or available > qty or code in sellable:
            raise OpenDError('Invalid, fractional or duplicate stock position')
        if qty:
            holdings[code[3:]] = qty
        sellable[code[3:]] = available
    if profile.get('environment') == 'REAL':
        if instrument_check is None:
            raise OpenDError('Live positions require broker instrument validation')
        instrument_check(tuple(holdings))
    suffix = 'live' if profile.get('environment') == 'REAL' else 'paper'
    value = Account(profile['provider'] + '-' + suffix + ':' + _identity(profile)[:20], 'USD', cash,
                    tuple(sorted(holdings.items())), started)
    return value, sellable


def _orders(sdk, client, profile, *, order_id='', day=None):
    kwargs = dict(_scope(sdk, profile), order_market=sdk.TrdMarket.NONE)
    if day is None:
        data = call(client, 'order_list_query', **kwargs, order_id=order_id, refresh_cache=True)
    else:
        data = call(client, 'history_order_list_query', **kwargs,
                    start=day + ' 00:00:00', end=day + ' 23:59:59')
    rows = records(data)
    ids = [str(r.get('order_id', '')) for r in rows]
    if len(rows) >= 1000 or len(set(ids)) != len(ids) or any(not x.isdigit() or int(x) <= 0 for x in ids):
        raise OpenDError('Ambiguous or oversized OpenD order response; reconcile manually')
    return rows


def instrument_validator(sdk, quote_client):
    """A short ticker alone cannot distinguish stocks from derivatives."""
    validated = set()
    def check(tickers):
        codes = sorted({_code(t) for t in tickers} - validated)
        if not codes:
            return
        if len(codes) > 200:
            raise OpenDError('Too many instruments for one validation request')
        rows = records(call(quote_client, 'get_stock_basicinfo', market=sdk.Market.US, code_list=codes))
        if len(rows) != len(codes) or {r.get('code') for r in rows} != set(codes):
            raise OpenDError('Incomplete broker instrument metadata')
        if any(r.get('stock_type') not in {sdk.SecurityType.STOCK, sdk.SecurityType.ETF}
               or r.get('delisting') is not False for r in rows):
            raise OpenDError('Only listed US stocks and ETFs are supported; derivatives are excluded')
        validated.update(codes)
    return check


class OpenDConnector:
    mode = 'broker_paper'
    environment = 'SIMULATE'
    provider = None  # Fixed by the registered platform subclass, never supplied by a strategy.

    def __init__(self, config_dir=None, state_dir=None):
        suffix = 'live' if self.environment == 'REAL' else 'paper'
        self.config_dir = ROOT / (config_dir or ('secrets/' + self.provider + ('_live' if suffix == 'live' else '')))
        self.state_dir = (ROOT / (state_dir or ('runs/' + self.provider + '-' + suffix))).resolve()
        self.state_files = (self.provider + '-' + suffix + '.sqlite',)

    def _settings(self):
        return settings(self.provider, self.config_dir, environment=self.environment)

    @contextmanager
    def _session(self, *, allow_orders=False, enable_live=False):
        with ExitStack() as stack:
            sdk, client, profile = stack.enter_context(connection(self._settings(), trade=True,
                allow_orders=allow_orders, enable_live=enable_live))
            validate = None
            if self.environment == 'REAL':
                _, quotes, quote_profile = stack.enter_context(connection(self._settings()))
                if quote_profile['user_id'] != profile['user_id']:
                    raise OpenDError('Trade and instrument-data connections must use the same login')
                validate = instrument_validator(sdk, quotes)
            yield sdk, client, profile, validate

    def inspect(self, *, connect=False):
        check = configuration_check(self.provider, self.config_dir, environment=self.environment)
        check.update(broker=self.provider + ('_live' if self.environment == 'REAL' else '_paper'))
        if not connect or check['issues']:
            return check
        with self._session() as (sdk, client, profile, validate):
            account, _ = _account(sdk, client, profile, lambda: datetime.now(timezone.utc), validate)
            active = [r for r in _orders(sdk, client, profile) if STATUSES.get(r.get('order_status')) not in TERMINAL]
            return dict(check, status='connected', mode='read_only', network_requested=True,
                        account_fingerprint=_identity(profile)[:20], reported_firm=profile.get('reported_firm'),
                        currency='USD', cash=str(account.cash), positions=[
                            {'symbol': s, 'quantity': str(q)} for s, q in account.positions],
                        open_order_count=len(active))

    def _path(self, state_dir):
        if Path(state_dir).resolve() != self.state_dir:
            raise OpenDError('Use the configured state directory; do not create another ledger')
        return self.state_dir / self.state_files[0]

    def initialize(self, state_dir, initial_cash):
        path = self._path(state_dir)
        # Verify environment/login/account before binding. Never reset broker funds.
        with connection(self._settings(), trade=True) as (_, _, profile):
            binding = _identity(profile)
        with path.open('xb'):
            pass
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE binding (identity_hash TEXT PRIMARY KEY);
                CREATE TABLE orders (client_id TEXT PRIMARY KEY, body TEXT NOT NULL,
                    created_at TEXT NOT NULL, broker_id TEXT UNIQUE, state TEXT);
            ''')
            db.execute('INSERT INTO binding VALUES (?)', (binding,))
            db.commit()

    def connect(self, state_dir, snapshot, fees, *, enable_live=False):
        if self.environment == 'REAL' and enable_live is not True:
            raise OpenDError('Live execution must be explicitly enabled before connecting')
        path = self._path(state_dir)
        manager = self._session(allow_orders=True, enable_live=enable_live)
        sdk, client, profile, validate = manager.__enter__()
        try:
            symbols = None if snapshot is None else {s.asset_id: s.ticker for s in snapshot.stocks}
            return OpenDBroker(sdk, client, profile, path, fees, symbol_map=symbols,
                                    enable_live=enable_live, instrument_check=validate,
                                    close_connection=lambda: manager.__exit__(None, None, None))
        except Exception:
            manager.__exit__(None, None, None)
            raise


class OpenDBroker:
    mode = 'broker_paper'

    def __init__(self, sdk, client, profile, path, fees=None, clock=None, symbol_map=None, close_connection=None,
                 *, enable_live=False, instrument_check=None):
        environment = profile.get('environment', 'SIMULATE')
        if (environment not in {'SIMULATE', 'REAL'}
                or str(getattr(client, '_verified_account', '')) != profile['account_id']
                or getattr(client, '_trading_environment', None) != environment
                or not getattr(client, '_allow_orders', False)):
            raise OpenDError('Verified account, matching environment and order permission required')
        if environment == 'REAL' and (enable_live is not True or not callable(instrument_check)):
            raise OpenDError('Live execution requires explicit activation and broker instrument validation')
        self.live_enabled = environment == 'REAL' and enable_live is True
        self.mode = 'live' if self.live_enabled else 'broker_paper'
        self.instrument_check = instrument_check
        self.sdk, self.client, self.profile = sdk, client, dict(profile)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fees, self.symbol_map = fees or Fees(), None if symbol_map is None else dict(symbol_map)
        self._close_connection = close_connection
        if self.symbol_map is not None and len(set(self.symbol_map.values())) != len(self.symbol_map):
            raise OpenDError('Ambiguous asset ID to ticker mapping')
        path = Path(path).resolve()
        if not path.is_file():
            raise OpenDError('Order ledger is missing; never recreate lost order state')
        self.db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=10)
        self.db.execute('PRAGMA synchronous=FULL')
        try:
            if self.db.execute('SELECT identity_hash FROM binding').fetchall() != [(_identity(profile),)]:
                raise OpenDError('Ledger belongs to a different provider, environment, login or account')
            self.db.execute('SELECT client_id,body,created_at,broker_id,state FROM orders LIMIT 0')
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise OpenDError('Ledger integrity check failed')
        except Exception:
            self.db.close()
            raise OpenDError('Invalid or mismatched ledger; do not delete it to retry') from None

    def close(self):
        self.db.close()
        if self._close_connection is not None:
            callback, self._close_connection = self._close_connection, None
            callback()

    def execution_time(self):
        return self.clock()

    def unlock(self, password):
        if self.mode != 'live' or not self.live_enabled:
            raise OpenDError('Only explicitly enabled live connections may unlock trading')
        call(self.client, 'unlock_trade', password=password)

    def account(self):
        value, _ = _account(self.sdk, self.client, self.profile, self.clock, self.instrument_check)
        if self.symbol_map is None:
            return value
        reverse = {ticker: asset for asset, ticker in self.symbol_map.items()}
        if any(t not in reverse for t, _ in value.positions):
            raise OpenDError('Snapshot must map every held stock to a stable asset ID')
        return Account(value.account_id, value.currency, value.cash,
                       tuple(sorted((reverse[t], q) for t, q in value.positions)), value.as_of)

    def _ticker(self, asset_id):
        if self.symbol_map is None:
            return asset_id
        if asset_id not in self.symbol_map:
            raise OpenDError('Missing ticker mapping for asset ID')
        return self.symbol_map[asset_id]

    def _tag(self, client_id):
        suffix = '-live' if self.mode == 'live' else ''
        return 'tb-' + self.profile['provider'] + suffix + '-' + sha256(client_id.encode()).hexdigest()[:40]

    @staticmethod
    def _decode(body):
        value = json.loads(body)
        ticker = value.pop('broker_symbol')
        return unpack_order(packed(value)), ticker

    def open_orders(self):
        ids = {str(r['order_id']) for r in _orders(self.sdk, self.client, self.profile)
               if STATUSES.get(r.get('order_status')) not in TERMINAL}
        for client_id, state in self.db.execute('SELECT client_id,state FROM orders').fetchall():
            if state is None or unpack_state(state).status not in TERMINAL:
                current = self.query(client_id)
                if current.status not in TERMINAL:
                    ids.add(current.broker_id or 'unknown:' + client_id)
        return tuple(sorted(ids))

    def _save(self, intent, ticker, row):
        if (row.get('code') != _code(ticker) or row.get('trd_side') != intent.side
                or row.get('order_type') != self.sdk.OrderType.NORMAL
                or _number(row.get('qty')) != intent.quantity
                or _number(row.get('price')) != intent.limit_price
                or row.get('remark') != self._tag(intent.client_id)
                or row.get('trd_env', _scope(self.sdk, self.profile)['trd_env']) != _scope(self.sdk, self.profile)['trd_env']
                or row.get('currency') != self.sdk.Currency.USD
                or row.get('time_in_force') != self.sdk.TimeInForce.DAY
                or row.get('fill_outside_rth') not in (False, 0)
                or row.get('session') != self.sdk.Session.RTH):
            raise OpenDError('OpenD order identity differs from the saved intent')
        fill, average = _number(row.get('dealt_qty')), _number(row.get('dealt_avg_price'))
        status = STATUSES.get(row.get('order_status'), 'unknown')
        # Order responses contain no commission. Cash is authoritative; zero here means unavailable.
        state = OrderState(intent.client_id, str(row['order_id']), status, fill, average)
        old_id, old_json = self.db.execute('SELECT broker_id,state FROM orders WHERE client_id=?', (intent.client_id,)).fetchone()
        old = unpack_state(old_json) if old_json else None
        if (not state.broker_id.isdigit() or int(state.broker_id) <= 0 or fill > intent.quantity
                or fill != fill.to_integral_value() or (status == 'filled' and fill != intent.quantity)
                or (status == 'partial' and not 0 < fill < intent.quantity)
                or (fill and (average <= 0 or (intent.side == 'BUY' and average > intent.limit_price)
                             or (intent.side == 'SELL' and average < intent.limit_price)))
                or (old_id and old_id != state.broker_id) or (old and old.filled_quantity > fill)
                or (old and old.status in TERMINAL and (status != old.status or fill != old.filled_quantity))):
            raise OpenDError('Inconsistent OpenD order identifier, fill or terminal state')
        with self.db:
            self.db.execute('UPDATE orders SET broker_id=?,state=? WHERE client_id=?',
                            (state.broker_id, packed(asdict(state)), intent.client_id))
        return state

    def query(self, client_id):
        record = self.db.execute('SELECT body,created_at,broker_id,state FROM orders WHERE client_id=?', (client_id,)).fetchone()
        if record is None:
            return OrderState(client_id, None, 'unknown', Decimal(0))
        body, created, broker_id, old_json = record
        intent, ticker = self._decode(body)
        def match(rows):
            return [r for r in rows if (str(r['order_id']) == broker_id if broker_id else r.get('remark') == self._tag(client_id))]
        found = match(_orders(self.sdk, self.client, self.profile, order_id=broker_id or ''))
        if not found:
            day = datetime.fromisoformat(created).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            found = match(_orders(self.sdk, self.client, self.profile, day=day))
        if len(found) > 1:
            raise OpenDError('Multiple broker orders match one intent; reconciliation required')
        if not found:
            old = unpack_state(old_json) if old_json else None
            return OrderState(client_id, broker_id, 'unknown', old.filled_quantity if old else Decimal(0),
                              old.average_price if old else Decimal(0))
        return self._save(intent, ticker, found[0])

    def submit(self, intent):
        ticker = self._ticker(intent.symbol)
        code = _code(ticker)
        body = packed(dict(asdict(intent), broker_symbol=ticker))
        prior = self.db.execute('SELECT body FROM orders WHERE client_id=?', (intent.client_id,)).fetchone()
        if prior:
            if prior[0] != body:
                raise OpenDError('Client ID was reused for a different order')
            return self.query(intent.client_id)
        qty, price = _number(intent.quantity, True), _number(intent.limit_price, True)
        if (not intent.client_id or intent.side not in {'BUY', 'SELL'} or qty != qty.to_integral_value()
                or qty * price > 100000 or price != price.quantize(Decimal('0.001'))):
            raise OpenDError('Only whole-share US limit orders up to USD 100000 and three price decimals are supported')
        if self.open_orders():
            raise OpenDError('Account has unresolved orders')
        if self.instrument_check is not None:
            self.instrument_check((ticker,))
        account, sellable = _account(self.sdk, self.client, self.profile, self.clock, self.instrument_check)
        if intent.side == 'BUY' and qty * price + self.fees.estimate(qty, price) > account.cash:
            raise OpenDError('Insufficient USD cash including estimated fees')
        if intent.side == 'SELL' and qty > sellable.get(ticker, Decimal(0)):
            raise OpenDError('Cannot sell unowned or unavailable shares')
        # Commit a transmission claim before calling the SDK. Crashes/timeout never trigger resends.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            claims = self.db.execute('SELECT client_id,state FROM orders').fetchall()
            if any(state is None or unpack_state(state).status not in TERMINAL for _, state in claims):
                raise OpenDError('Another transmission is unresolved')
            self.db.execute('INSERT INTO orders(client_id,body,created_at) VALUES (?,?,?)',
                            (intent.client_id, body, self.clock().isoformat()))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise OpenDError('Order claim failed or already exists; reconcile before retry') from None
        try:
            reply = records(call(self.client, 'place_order', **_scope(self.sdk, self.profile),
                price=float(price), qty=int(qty), code=code, trd_side=intent.side,
                order_type=self.sdk.OrderType.NORMAL, adjust_limit=0, remark=self._tag(intent.client_id),
                time_in_force=self.sdk.TimeInForce.DAY, fill_outside_rth=False, session=self.sdk.Session.RTH))
            if len(reply) == 1:
                broker_id = str(reply[0].get('order_id', ''))
                if broker_id.isdigit() and int(broker_id) > 0:
                    with self.db:
                        self.db.execute('UPDATE orders SET broker_id=? WHERE client_id=?', (broker_id, intent.client_id))
        except OpenDError:
            # Includes uncertain rejection/error replies: only a subsequent query can resolve them.
            return OrderState(intent.client_id, None, 'unknown', Decimal(0))
        return self.query(intent.client_id)

    def cancel(self, broker_id):
        row = self.db.execute('SELECT client_id FROM orders WHERE broker_id=?', (str(broker_id),)).fetchone()
        if row is None:
            raise OpenDError('Only orders in this adapter ledger can be cancelled')
        state = self.query(row[0])
        if state.status in TERMINAL or state.status == 'unknown':
            return state
        call(self.client, 'modify_order', **_scope(self.sdk, self.profile),
             modify_order_op=self.sdk.ModifyOrderOp.CANCEL, order_id=str(broker_id), qty=0, price=0)
        # An acknowledgement is not confirmation of cancellation; fills can still arrive.
        return self.query(row[0])
