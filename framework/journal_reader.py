"""Read-only order views; consumers never depend on SQLite tables or JSON columns."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo


class JournalReadError(ValueError):
    pass


class JournalReader:
    def __init__(self, path, *, broker_ledger_path=None, envelope_dir=None):
        self.path = Path(path).resolve()
        self.broker_ledger_path = Path(broker_ledger_path).resolve() if broker_ledger_path else None
        self.envelope_dir = Path(envelope_dir).resolve() if envelope_dir else None

    def _rows(self, query, parameters=()):
        if not self.path.is_file():
            return None
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                return db.execute(query, parameters).fetchall()
        except sqlite3.Error:
            raise JournalReadError('账户账本无法核对，请保留原文件并检查。') from None

    def has_unresolved(self):
        return bool(self._rows("SELECT 1 FROM intents WHERE status NOT IN ('filled','cancelled','rejected','abandoned') LIMIT 1"))

    @staticmethod
    def _valid_ticker(value):
        return isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', value) is not None

    def _broker_tickers(self, orders):
        """Read only the selected account's durable adapter records, without an SDK."""
        result = {}
        path = self.broker_ledger_path
        if path is None or not path.is_file():
            return result
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                for ident, order in orders.items():
                    if order.get('client_id') != ident:
                        continue
                    row = db.execute('SELECT body FROM orders WHERE client_id=?', (ident,)).fetchone()
                    if row is None:
                        continue
                    saved = json.loads(row[0])
                    ticker = saved.pop('broker_symbol', None)
                    # A reused client ID is not evidence for this historical order.
                    if saved == order and self._valid_ticker(ticker):
                        result[ident] = ticker
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError):
            # Missing display metadata must never hide the actual order history.
            return {}
        return result

    def _envelope_tickers(self, orders, cycles):
        """Use mappings only from persisted envelopes matching a whole frozen batch."""
        result = {}
        if self.envelope_dir is None or not self.envelope_dir.is_dir():
            return result
        batches = {}
        try:
            for cycle in set(cycles.values()):
                rows = self._rows('SELECT body FROM batches WHERE cycle=?', (cycle,))
                if rows:
                    batches[cycle] = json.loads(rows[0][0])
        except (JournalReadError, ValueError, TypeError):
            return result
        if not batches:
            return result
        try:
            paths = self.envelope_dir.glob('*.json')
            for path in paths:
                try:
                    # Do not follow an envelope symlink into unrelated files.
                    if path.resolve().parent != self.envelope_dir or path.stat().st_size > 32 * 1024 * 1024:
                        continue
                    raw = json.loads(path.read_text(encoding='utf-8-sig'))
                    if raw.get('schema') != 'paper_order_preparation_v1':
                        continue
                    stamp = datetime.fromisoformat(raw['execution_time'])
                    if stamp.tzinfo is None:
                        continue
                    day = stamp.astimezone(ZoneInfo('America/New_York')).date().isoformat()
                    cycle = raw['account']['account_id'] + ':' + day
                    if cycle not in batches or raw['orders'] != batches[cycle]:
                        continue
                    mapping = raw['instruments']
                    for order in raw['orders']:
                        ident = order['client_id']
                        if orders.get(ident) != order or cycles.get(ident) != cycle:
                            continue
                        ticker = mapping.get(order['symbol'])
                        if self._valid_ticker(ticker):
                            result.setdefault(ident, set()).add(ticker)
                except (OSError, ValueError, TypeError, AttributeError, KeyError):
                    continue
        except OSError:
            return result
        return result

    def recent(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid order view limit')
        rows = self._rows('SELECT id, cycle, body, status, state FROM intents ORDER BY rowid DESC LIMIT ?', (limit,))
        if rows is None:
            return {'items': [], 'message': '当前账户模块暂无本程序的订单记录。'}
        try:
            items = []
            orders, cycles = {}, {}
            for ident, cycle, body, status, state in rows:
                order, fill = json.loads(body), json.loads(state) if state else {}
                orders[ident], cycles[ident] = order, cycle
                items.append({'id': ident, 'cycle': cycle, 'symbol': order.get('symbol'),
                              'asset_id': order.get('symbol'),
                              'side': order.get('side'), 'quantity': order.get('quantity'),
                              'limit_price': order.get('limit_price'), 'status': status,
                              'filled_quantity': fill.get('filled_quantity'), 'average_price': fill.get('average_price')})
            broker_tickers = self._broker_tickers(orders)
            envelope_tickers = self._envelope_tickers(orders, cycles)
            for item in items:
                ident = item['id']
                candidates = set(envelope_tickers.get(ident, ()))
                if ident in broker_tickers:
                    candidates.add(broker_tickers[ident])
                ticker = next(iter(candidates)) if len(candidates) == 1 else None
                item.update(ticker=ticker,
                            ticker_source=('broker_ledger' if ident in broker_tickers else 'frozen_envelope') if ticker else None,
                            ticker_status='resolved' if ticker else ('conflict' if candidates else 'unavailable'),
                            display_symbol=ticker or ('内部标识：' + str(item['asset_id'] or '未知')))
            return {'items': items, 'message': f'最近 {limit} 条本程序订单记录；券商当前状态需通过连接检查核对。'}
        except (ValueError, TypeError, AttributeError):
            raise JournalReadError('账本记录无法解析，请保留原文件并核对。') from None
