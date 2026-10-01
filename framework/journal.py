"""SQLite intent journal. Transactions finish before any broker transmission."""
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import os
from framework.contracts import OrderIntent, OrderState


def packed(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def unpack_order(body):
    data = json.loads(body)
    data["quantity"], data["limit_price"] = Decimal(data["quantity"]), Decimal(data["limit_price"])
    return OrderIntent(**data)


def unpack_state(body):
    data = json.loads(body)
    for key in ("filled_quantity", "average_price", "fees"):
        data[key] = Decimal(data[key])
    return OrderState(**data)


class Journal:
    def __init__(self, path):
        path = Path(path)
        self.directory = path.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.exists()
        self.db = sqlite3.connect(path, timeout=10)
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.db.execute("PRAGMA synchronous=FULL")
        if existing:
            try:
                self.db.execute("SELECT cycle,fingerprint,body FROM batches LIMIT 0")
                self.db.execute("SELECT id,cycle,body,status,state FROM intents LIMIT 0")
                if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Journal integrity check failed")
            except (sqlite3.DatabaseError, ValueError) as exc:
                self.db.close()
                raise ValueError("Existing journal is invalid; never initialize over lost state") from exc
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                cycle TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (
                id TEXT PRIMARY KEY, cycle TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, state TEXT);
        """)

    def close(self):
        self.db.close()

    def record_decision(self, decision):
        path = self.directory / ("decision-" + decision.decision_id + ".json")
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(decision.record, handle, indent=2, ensure_ascii=False, allow_nan=False)
        except FileExistsError:
            if json.loads(path.read_text(encoding="utf-8")) != decision.record:
                raise ValueError("Saved decision differs from the current decision")

    @contextmanager
    def exclusive(self):
        # OS releases the lock on crashes. One writer per dedicated account journal.
        with self.lock_path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def batch(self, cycle):
        row = self.db.execute("SELECT fingerprint,body FROM batches WHERE cycle=?", (cycle,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def freeze(self, cycle, fingerprint, orders):
        body = packed([asdict(o) for o in orders])
        with self.transaction():
            row = self.db.execute("SELECT fingerprint FROM batches WHERE cycle=?", (cycle,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("Cycle already frozen with different decision/configuration")
                return
            self.db.execute("INSERT INTO batches VALUES (?,?,?)", (cycle, fingerprint, body))
            for order in orders:
                self.db.execute("INSERT INTO intents VALUES (?,?,?,?,NULL)",
                                (order.client_id, cycle, packed(asdict(order)), "planned"))

    def claim(self, order):
        with self.transaction():
            cursor = self.db.execute("UPDATE intents SET status='unknown' WHERE id=? AND status='planned'",
                                     (order.client_id,))
            return cursor.rowcount == 1

    def status(self, order):
        row = self.db.execute("SELECT status,state FROM intents WHERE id=?", (order.client_id,)).fetchone()
        if row is None:
            raise ValueError("Missing intent")
        return row[0], unpack_state(row[1]) if row[1] else None

    def save(self, state):
        with self.transaction():
            self.db.execute("UPDATE intents SET status=?,state=? WHERE id=?",
                            (state.status, packed(asdict(state)), state.client_id))

    def other_unfinished(self, cycle):
        return self.db.execute("SELECT id FROM intents WHERE cycle<>? AND status<>'filled' LIMIT 1",
                               (cycle,)).fetchone() is not None

    def has_later_cycle(self, cycle):
        prefix = cycle.rsplit(":", 1)[0] + ":%"
        return self.db.execute("SELECT cycle FROM batches WHERE cycle LIKE ? AND cycle>? LIMIT 1",
                               (prefix, cycle)).fetchone() is not None
