"""Read-only order views; consumers never depend on SQLite tables or JSON columns."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


class JournalReadError(ValueError):
    pass


class JournalReader:
    def __init__(self, path):
        self.path = Path(path).resolve()

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

    def recent(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid order view limit')
        rows = self._rows('SELECT id, cycle, body, status, state FROM intents ORDER BY rowid DESC LIMIT ?', (limit,))
        if rows is None:
            return {'items': [], 'message': '当前账户模块暂无本程序的订单记录。'}
        try:
            items = []
            for ident, cycle, body, status, state in rows:
                order, fill = json.loads(body), json.loads(state) if state else {}
                items.append({'id': ident, 'cycle': cycle, 'symbol': order.get('symbol'),
                              'side': order.get('side'), 'quantity': order.get('quantity'),
                              'limit_price': order.get('limit_price'), 'status': status,
                              'filled_quantity': fill.get('filled_quantity'), 'average_price': fill.get('average_price')})
            return {'items': items, 'message': f'最近 {limit} 条本程序订单记录；券商当前状态需通过连接检查核对。'}
        except (ValueError, TypeError, AttributeError):
            raise JournalReadError('账本记录无法解析，请保留原文件并核对。') from None
