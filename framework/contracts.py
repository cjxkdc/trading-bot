"""Research and order contracts. Concrete adapters live in data/ and brokers/."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol, Sequence

History = Sequence[tuple[date, float]]


class SubmissionRejected(RuntimeError):
    """Adapter proves this new intent failed before its transmission claim.

    Never raise this for an existing adapter record or after a send might have
    happened. Unknown historical orders cannot be reclassified with this type.
    """
    error_code = 'broker_pre_submit_rejected'


class SubmissionNotAttempted(RuntimeError):
    """Adapter proves it stopped before any durable record or broker call.

    Raised for conditions that are not a verdict on the order itself: a failed
    account or open-order query, unresolved earlier orders, or a cash/position
    check that disagrees with the executor's own. The executor releases the
    intent back to planned so the batch can be reconciled and continued; a
    permanent rejection would wrongly retire it. Never raise this after a send
    might have happened.
    """
    error_code = 'broker_submission_not_attempted'


class Strategy(Protocol):
    @property
    def warmup(self) -> int: ...

    def target(self, history: History) -> float:
        """Return exposure in [0,1]; history contains only known completed bars."""
        ...


class HistoricalData(Protocol):
    def load(self) -> History: ...


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    limit_price: Decimal


@dataclass(frozen=True)
class OrderState:
    client_id: str
    broker_id: str | None
    status: Literal["pending", "accepted", "partial", "filled", "cancelled", "rejected", "unknown"]
    filled_quantity: Decimal
    average_price: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")


class Broker(Protocol):
    """Order boundary shared by local replay, simulation and activated live adapters.

    Unknown submission must be reconciled before retry. Cancel acknowledgement
    does not prove no further fills. Strategies never receive a Broker instance.
    """
    def submit(self, order: OrderIntent) -> OrderState: ...
    def query(self, client_id: str) -> OrderState: ...
    def cancel(self, broker_id: str) -> OrderState: ...
