"""One portfolio workflow, supplied with strategies, data and brokers by assembly."""
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo
from framework.decisions import decide
from framework.execution import Executor
from framework.portfolio import PortfolioContext, StockSnapshot
from framework.trading import Quote


@dataclass(frozen=True)
class MarketSnapshot:
    input_schema: str
    stocks: tuple[StockSnapshot, ...]
    quotes: Mapping[str, Quote]
    execution_time: datetime
    portfolio_context: PortfolioContext | None = None


class SnapshotProvider(Protocol):
    def load(self) -> MarketSnapshot: ...


class BrokerConnector(Protocol):
    """Lifecycle is adapter-specific; order operations use ExecutionBroker."""
    mode: str
    state_files: tuple[str, ...]

    def initialize(self, state_dir, initial_cash): ...
    def connect(self, state_dir, snapshot, fees): ...


class PortfolioRunner:
    def __init__(self, engine, strategy_spec, input_schema, policy, fees):
        self.engine, self.strategy_spec = engine, strategy_spec
        self.input_schema, self.policy, self.fees = input_schema, policy, fees

    def plan(self, snapshot: MarketSnapshot):
        if snapshot.input_schema != self.input_schema:
            raise ValueError("Strategy and data input schemas do not match")
        if snapshot.execution_time.tzinfo is None:
            raise ValueError("Execution timestamp must include timezone")
        execution_day = snapshot.execution_time.astimezone(ZoneInfo("America/New_York")).date()
        return decide(self.engine, snapshot.stocks, execution_day, self.strategy_spec,
                      input_schema=self.input_schema, context=snapshot.portfolio_context)

    def execute(self, decision, snapshot, broker, journal):
        # Recompute before execution so a preview cannot be paired with other data.
        if decision != self.plan(snapshot):
            raise ValueError("Preview does not match the supplied market snapshot")
        journal.record_decision(decision)
        result = Executor(broker, journal, self.policy, self.fees).run(
            decision, snapshot.quotes, snapshot.execution_time)
        return dict(result, account=asdict(broker.account()))
