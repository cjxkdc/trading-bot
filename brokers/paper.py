"""Durable local quote-replay broker. No network, settlement or exchange simulator."""
from dataclasses import asdict
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import uuid
from framework.contracts import OrderState, SubmissionRejected
from framework.journal import packed, unpack_state
from framework.trading import Account, amount, fresh


class PaperSubmissionRejected(SubmissionRejected, ValueError):
    """Local pre-checks failed before anything was recorded; the journal records a rejection."""


class PaperConnector:
    """Explicit lifecycle for the dedicated local account; never contacts a broker."""
    mode = "paper"
    state_files = ("paper.sqlite",)

    def initialize(self, state_dir, initial_cash):
        PaperBroker.create(Path(state_dir) / self.state_files[0], initial_cash)

    def connect(self, state_dir, snapshot, fees, max_quote_age_seconds=120):
        return PaperBroker(Path(state_dir) / self.state_files[0], snapshot.quotes,
                           snapshot.execution_time, fees, max_quote_age_seconds=max_quote_age_seconds)


class PaperBroker:
    mode = "paper"

    @classmethod
    def create(cls, path, cash):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents an accidental account reset.
        with path.open("xb"):
            pass
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE account (id TEXT PRIMARY KEY, cash TEXT NOT NULL);
                CREATE TABLE positions (symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL);
                CREATE TABLE orders (client_id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL);
            """)
            db.execute("INSERT INTO account VALUES (?,?)", ("paper-" + uuid.uuid4().hex, str(amount(cash, True))))
            db.commit()

    def __init__(self, path, quotes, now, fees, max_quote_age_seconds=120):
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError("Paper account missing; explicit initialization required")
        self.db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
        self.db.execute("PRAGMA synchronous=FULL")
        self.quotes, self.now, self.fees = quotes, now, fees
        # Shared with the execution policy so the replay never rejects a quote
        # that the executor's own freshness check just accepted.
        self.max_quote_age_seconds = max_quote_age_seconds
        self.account()  # Detect corrupt or uninitialized accounts immediately.

    def close(self):
        self.db.close()

    def account(self):
        rows = self.db.execute("SELECT id,cash FROM account").fetchall()
        if len(rows) != 1:
            raise ValueError("Invalid paper account")
        positions = tuple((s, Decimal(q)) for s, q in self.db.execute("SELECT symbol,quantity FROM positions ORDER BY symbol"))
        return Account(rows[0][0], "USD", Decimal(rows[0][1]), positions, self.now)

    def query(self, client_id):
        row = self.db.execute("SELECT state FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return unpack_state(row[0]) if row else OrderState(client_id, None, "unknown", Decimal(0))

    def open_orders(self):
        states = [unpack_state(row[0]) for row in self.db.execute("SELECT state FROM orders")]
        return tuple(s for s in states if s.status not in {"filled", "cancelled", "rejected"})

    def submit(self, order):
        body = packed(asdict(order))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT body,state FROM orders WHERE client_id=?", (order.client_id,)).fetchone()
            if prior:
                if prior[0] != body:
                    raise ValueError("Reused client ID with a different order")
                self.db.commit()
                return unpack_state(prior[1])
            # Pre-checks happen before any row exists. They raise the explicit
            # pre-submission rejection so the journal never leaves the intent
            # stranded as 'unknown' for an order that was never "sent".
            try:
                quote = self.quotes[order.symbol]
                fresh(quote.as_of, self.now, self.max_quote_age_seconds)
                if amount(quote.bid, True) > amount(quote.ask, True):
                    raise ValueError("Crossed quote")
                amount(order.limit_price, True)
                if order.quantity != amount(order.quantity, True).to_integral_value():
                    raise ValueError("Whole shares required")
                if order.side not in {"BUY", "SELL"}:
                    raise ValueError("Unsupported side")
                price = quote.ask if order.side == "BUY" else quote.bid
                if (order.side == "BUY" and order.limit_price < price
                        or order.side == "SELL" and order.limit_price > price):
                    raise ValueError("Non-marketable orders are outside the local replay model")
            except KeyError:
                raise PaperSubmissionRejected("Missing replay quote for the order symbol") from None
            except ValueError as exc:
                raise PaperSubmissionRejected(str(exc)) from None
            account = self.account()
            positions = dict(account.positions)
            held = positions.get(order.symbol, Decimal(0))
            fee = self.fees.estimate(order.quantity, price)
            cash = account.cash + (1 if order.side == "SELL" else -1) * order.quantity * price - fee
            quantity = held + (1 if order.side == "BUY" else -1) * order.quantity
            if cash < 0 or quantity < 0:
                state = OrderState(order.client_id, "paper:" + order.client_id, "rejected", Decimal(0))
            else:
                state = OrderState(order.client_id, "paper:" + order.client_id, "filled", order.quantity, price, fee)
                self.db.execute("UPDATE account SET cash=?", (str(cash),))
                if quantity:
                    self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?)", (order.symbol, str(quantity)))
                else:
                    self.db.execute("DELETE FROM positions WHERE symbol=?", (order.symbol,))
            self.db.execute("INSERT INTO orders VALUES (?,?,?)", (order.client_id, body, packed(asdict(state))))
            self.db.commit()
            return state
        except BaseException:
            self.db.rollback()
            raise

    def cancel(self, broker_id):
        if not broker_id.startswith("paper:"):
            raise ValueError("Unknown paper order ID")
        # This model fills synchronously. A cancellation cannot undo a fill.
        return self.query(broker_id[len("paper:"):])
