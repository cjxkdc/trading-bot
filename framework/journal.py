"""SQLite intent journal. Transactions finish before any broker transmission."""
from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal
from datetime import datetime, timezone
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
        self._submission_claims = {}
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
        if getattr(self, '_exclusive_held', False):
            raise RuntimeError('Journal exclusive lock is not reentrant')
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
                self._exclusive_held = True
                yield
            finally:
                self._submission_claims.clear()
                self._exclusive_held = False
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
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Submission claim requires the exclusive journal lock')
        with self.transaction():
            cursor = self.db.execute("UPDATE intents SET status='unknown' WHERE id=? AND status='planned'",
                                     (order.client_id,))
            claimed = cursor.rowcount == 1
        if not claimed:
            return None
        token = object()
        self._submission_claims[order.client_id] = token
        return token

    def release_claim(self, order, token):
        """Forget the in-process proof after this one submission attempt."""
        if self._submission_claims.get(order.client_id) is token:
            self._submission_claims.pop(order.client_id, None)

    def status(self, order):
        row = self.db.execute("SELECT status,state FROM intents WHERE id=?", (order.client_id,)).fetchone()
        if row is None:
            raise ValueError("Missing intent")
        return row[0], unpack_state(row[1]) if row[1] else None

    def reject_unsubmitted(self, order, token):
        """Record proof supplied only by the current adapter submission call.

        This is not a migration/recovery operation. The exclusive executor has
        just claimed this exact new intent; no historical unknown is changed.
        """
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Pre-submit rejection requires the exclusive journal lock')
        if token is None or self._submission_claims.get(order.client_id) is not token:
            raise ValueError('Pre-submit rejection requires this process current submission claim')
        state = OrderState(order.client_id, None, 'rejected', Decimal(0))
        with self.transaction():
            row = self.db.execute('SELECT body,status,state FROM intents WHERE id=?',
                                  (order.client_id,)).fetchone()
            if row != (packed(asdict(order)), 'unknown', None):
                raise ValueError('Pre-submit rejection requires the unchanged new claim')
            self.db.execute('''CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL)''')
            self.db.execute('INSERT INTO execution_audit(recorded_at,action,body) VALUES(?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), 'reject_before_transmission',
                             packed({'client_id': order.client_id, 'error_code': 'broker_pre_submit_rejected',
                                     'transmission_started': False})))
            self.db.execute('UPDATE intents SET status=?,state=? WHERE id=?',
                            ('rejected', packed(asdict(state)), order.client_id))

    def unclaim(self, order, token):
        """Release a claim whose adapter proved nothing was recorded or sent.

        Mirrors reject_unsubmitted: only the current in-process claim on this
        exact, otherwise untouched intent can be released. The intent returns to
        planned with an audit row; nothing is resent here.
        """
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Releasing a claim requires the exclusive journal lock')
        if token is None or self._submission_claims.get(order.client_id) is not token:
            raise ValueError('Releasing a claim requires this process current submission claim')
        with self.transaction():
            row = self.db.execute('SELECT body,status,state FROM intents WHERE id=?',
                                  (order.client_id,)).fetchone()
            if row != (packed(asdict(order)), 'unknown', None):
                raise ValueError('Releasing a claim requires the unchanged new claim')
            self.db.execute('''CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL)''')
            self.db.execute('INSERT INTO execution_audit(recorded_at,action,body) VALUES(?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), 'release_unattempted_claim',
                             packed({'client_id': order.client_id, 'error_code': 'broker_submission_not_attempted',
                                     'transmission_started': False})))
            self.db.execute("UPDATE intents SET status='planned' WHERE id=? AND status='unknown'", (order.client_id,))

    def close_untransmitted(self, account_id, client_ids, evidence):
        """Retire unknown intents that provably never reached the broker.

        The caller holds the exclusive lock, re-queried the broker through a
        read-only session, supplies per-intent evidence and has confirmed the
        check at the broker. Saved states stay; only the ledger status moves to
        'closed', with an audit row holding the evidence.
        """
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Closing untransmitted intents requires the exclusive journal lock')
        wanted = set(client_ids)
        if not wanted or wanted != set(evidence):
            raise ValueError('Every closed intent needs broker evidence')
        summaries = self.cycle_statuses()
        if any(cycle.rsplit(':', 1)[0] != account_id for cycle in summaries):
            raise ValueError('Journal belongs to another account')
        rows = [row for row in self.db.execute(
            "SELECT id,cycle,body,status,state FROM intents WHERE status='unknown' ORDER BY rowid").fetchall()
            if row[0] in wanted]
        if {row[0] for row in rows} != wanted:
            raise ValueError('Untransmitted intents changed while closing')
        for ident, cycle, body, status, state in rows:
            saved = unpack_state(state) if state else None
            if saved is not None and (saved.broker_id or saved.filled_quantity):
                raise ValueError('Intent has broker evidence; it cannot be closed as untransmitted')
        with self.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL)''')
            event = {'account_id': account_id, 'intents': [
                {'client_id': ident, 'cycle': cycle, 'intent': json.loads(body),
                 'state': json.loads(state) if state else None, 'evidence': evidence[ident]}
                for ident, cycle, body, status, state in rows]}
            self.db.execute('INSERT INTO execution_audit(recorded_at,action,body) VALUES(?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), 'close_untransmitted', packed(event)))
            closed = 0
            for ident, *_ in rows:
                closed += self.db.execute("UPDATE intents SET status='closed' WHERE id=? AND status='unknown'", (ident,)).rowcount
            if closed != len(rows):
                raise ValueError('Intent state changed while closing untransmitted intents')
        return closed

    def rejected_before_transmission(self, order):
        """Only the durable adapter rejection proof permits skipping a broker query."""
        expected = OrderState(order.client_id, None, 'rejected', Decimal(0))
        if self.status(order) != ('rejected', expected):
            return False
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_audit'").fetchone():
            return False
        proof = packed({'client_id': order.client_id, 'error_code': 'broker_pre_submit_rejected',
                        'transmission_started': False})
        return self.db.execute('SELECT 1 FROM execution_audit WHERE action=? AND body=? LIMIT 1',
                               ('reject_before_transmission', proof)).fetchone() is not None

    def unfinished_orders(self, limit=1000):
        """Return durable incomplete intents without leaking database layout to callers."""
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('Invalid reconciliation limit')
        rows = self.db.execute(
            "SELECT cycle,body,status FROM intents WHERE status NOT IN ('filled','abandoned','closed') ORDER BY rowid LIMIT ?",
            (limit+1,)).fetchall()
        if len(rows) > limit:
            raise ValueError('Too many unresolved intents; manual reconciliation required')
        return tuple((cycle, unpack_order(body), status) for cycle, body, status in rows)

    def cycle_statuses(self):
        """A frozen batch exists even when nothing reached the broker yet."""
        rows = self.db.execute('''
            SELECT batches.cycle, intents.status, COUNT(intents.id)
            FROM batches LEFT JOIN intents ON batches.cycle=intents.cycle
            GROUP BY batches.cycle,intents.status ORDER BY batches.cycle
        ''').fetchall()
        cycles = {}
        for cycle, status, count in rows:
            value = cycles.setdefault(cycle, {})
            if status is not None:
                value[status] = count
        for cycle, body in self.db.execute('SELECT cycle,body FROM batches'):
            expected = json.loads(body)
            actual = [json.loads(row[0]) for row in self.db.execute(
                'SELECT body FROM intents WHERE cycle=? ORDER BY rowid', (cycle,))]
            if expected != actual:
                raise ValueError('Frozen batch and intent ledger disagree; reconciliation required')
        return cycles

    def save(self, state):
        with self.transaction():
            self.db.execute("UPDATE intents SET status=?,state=? WHERE id=?",
                            (state.status, packed(asdict(state)), state.client_id))

    def abandon_planned(self, account_id):
        """Explicitly retire only never-claimed intents, keeping frozen bodies and audit.

        Caller must hold the execution lock and have reconciled the broker through
        a read-only connection immediately before this operation.
        """
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Abandonment requires the exclusive journal lock')
        summaries = self.cycle_statuses()
        if any(cycle.rsplit(':', 1)[0] != account_id for cycle in summaries):
            raise ValueError('Journal belongs to another account')
        if any(set(counts)-{'filled', 'planned', 'abandoned', 'closed'} for counts in summaries.values()):
            raise ValueError('Only completely filled and never-submitted orders can be closed')
        rows = self.db.execute("SELECT id,cycle,body FROM intents WHERE status='planned' ORDER BY rowid").fetchall()
        if not rows:
            return 0
        with self.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL)''')
            event = {'account_id': account_id, 'intents': [
                {'client_id': ident, 'cycle': cycle, 'intent': json.loads(body)} for ident, cycle, body in rows]}
            self.db.execute('INSERT INTO execution_audit(recorded_at,action,body) VALUES(?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), 'abandon_unsubmitted', packed(event)))
            changed = self.db.execute("UPDATE intents SET status='abandoned' WHERE status='planned'").rowcount
            if changed != len(rows):
                raise ValueError('Intent state changed while closing the batch')
        return changed

    def close_terminal(self, account_id):
        """Retire broker-confirmed cancelled/rejected intents and the planned remainder.

        The user explicitly confirms that these terminal orders were checked. Saved
        broker states and filled quantities are kept; only the ledger status moves
        to 'closed' (or 'abandoned' for never-claimed intents), with an audit row.
        Caller must hold the execution lock and have re-queried the broker.
        """
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Closing terminal orders requires the exclusive journal lock')
        summaries = self.cycle_statuses()
        if any(cycle.rsplit(':', 1)[0] != account_id for cycle in summaries):
            raise ValueError('Journal belongs to another account')
        allowed = {'filled', 'planned', 'abandoned', 'closed', 'cancelled', 'rejected'}
        if any(set(counts)-allowed for counts in summaries.values()):
            raise ValueError('Unknown, pending or partial orders must be reconciled before closing')
        rows = self.db.execute(
            "SELECT id,cycle,body,status,state FROM intents WHERE status IN ('cancelled','rejected','planned') ORDER BY rowid").fetchall()
        if not any(status in {'cancelled', 'rejected'} for *_, status, _ in rows):
            return {'closed': 0, 'abandoned': 0}
        with self.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS execution_audit (
                id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL)''')
            event = {'account_id': account_id, 'intents': [
                {'client_id': ident, 'cycle': cycle, 'status': status, 'intent': json.loads(body),
                 'state': json.loads(state) if state else None} for ident, cycle, body, status, state in rows]}
            self.db.execute('INSERT INTO execution_audit(recorded_at,action,body) VALUES(?,?,?)',
                            (datetime.now(timezone.utc).isoformat(), 'close_terminal', packed(event)))
            closed = self.db.execute("UPDATE intents SET status='closed' WHERE status IN ('cancelled','rejected')").rowcount
            abandoned = self.db.execute("UPDATE intents SET status='abandoned' WHERE status='planned'").rowcount
            if closed + abandoned != len(rows):
                raise ValueError('Intent state changed while closing the batch')
        return {'closed': closed, 'abandoned': abandoned}

    def other_unfinished(self, cycle):
        return self.db.execute("SELECT id FROM intents WHERE cycle<>? AND status NOT IN ('filled','abandoned','closed') LIMIT 1",
                               (cycle,)).fetchone() is not None

    def has_later_cycle(self, cycle):
        prefix = cycle.rsplit(":", 1)[0] + ":%"
        return self.db.execute("SELECT cycle FROM batches WHERE cycle LIKE ? AND cycle>? LIMIT 1",
                               (prefix, cycle)).fetchone() is not None
