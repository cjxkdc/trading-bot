"""Durable operational events and a bounded, at-least-once notification outbox."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
import time


class EventStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, at TEXT, severity TEXT, message TEXT, next_action TEXT,
                delivery TEXT, attempts INTEGER, next_attempt REAL);
                CREATE TABLE IF NOT EXISTS health (id TEXT PRIMARY KEY, healthy INTEGER);''')
            db.execute("UPDATE events SET delivery='pending' WHERE delivery='sending'")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def emit(self, key, message, severity='info', next_action=None, *, notify=False, at=None):
        ident = sha256(key.encode()).hexdigest()[:32]
        at = at or datetime.now(timezone.utc).isoformat()
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,0,0)',
                       (ident, at, severity, str(message)[:800], next_action, 'pending' if notify else 'local'))
        return ident

    def transition(self, key, healthy, message, next_action=None, *, notify=False):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT healthy FROM health WHERE id=?', (key,)).fetchone()
            if old and bool(old[0]) == bool(healthy):
                return
            db.execute('INSERT OR REPLACE INTO health VALUES(?,?)', (key,int(healthy)))
            # State and its notification commit together, including across restarts.
            if old or not healthy:
                at = datetime.now(timezone.utc).isoformat()
                ident = sha256((key+at).encode()).hexdigest()[:32]
                db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,0,0)',
                    (ident, at, 'info' if healthy else 'error', str(message)[:800],
                     next_action, 'pending' if notify else 'local'))

    def recent(self, limit=80):
        with closing(self.connect()) as db:
            return [dict(zip(('id','at','severity','message','next_action','delivery','attempts'), row))
                    for row in db.execute('SELECT id,at,severity,message,next_action,delivery,attempts FROM events ORDER BY at DESC LIMIT ?', (limit,))]

    def deliver_one(self, send, *, now=None):
        now = time.time() if now is None else now
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,at,severity,message,next_action,attempts FROM events WHERE delivery='pending' AND next_attempt<=? ORDER BY at LIMIT 1", (now,)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE events SET delivery='sending' WHERE id=?", (row[0],))
        event = dict(zip(('id','at','severity','message','next_action','attempts'), row))
        try:
            send(event)
            status = 'delivered'
        except Exception:
            status = 'failed' if event['attempts']+1 >= 3 else 'pending'
        with closing(self.connect()) as db, db:
            db.execute('UPDATE events SET delivery=?,attempts=attempts+1,next_attempt=? WHERE id=?',
                       (status, now+60*2**event['attempts'], event['id']))
        return True

    def cancel_pending(self):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE events SET delivery='cancelled' WHERE delivery='pending'")
