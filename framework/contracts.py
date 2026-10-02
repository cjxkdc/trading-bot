"""Research and order contracts. Concrete adapters remain in platforms/."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol, Sequence

History = Sequence[tuple[date, float]]


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
