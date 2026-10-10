"""One portfolio workflow, supplied with strategies, data and brokers by assembly."""
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo
from framework.decisions import decide
from framework.execution import Executor
from framework.portfolio import PortfolioContext, StockSnapshot
from framework.trading import Quote
from framework.market import ExecutionContext, execution_context


@dataclass(frozen=True)
class MarketSnapshot:
    input_schema: str
    stocks: tuple[StockSnapshot, ...]
    quotes: Mapping[str, Quote]
    execution_time: datetime
    portfolio_context: PortfolioContext | None = None
    current_information: bool = False
    instruments: Mapping[str, str] | None = None
    clock_offset_seconds: float = 0.
    execution_context: ExecutionContext | Mapping | None = None

    @property
    def market(self):
        return execution_context(self.execution_context)


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
        context = execution_context(snapshot.execution_context)
        execution_day = snapshot.execution_time.astimezone(ZoneInfo(context.timezone)).date()
        return decide(self.engine, snapshot.stocks, execution_day, self.strategy_spec,
                      input_schema=self.input_schema, context=snapshot.portfolio_context,
                      current_information=snapshot.current_information,
                      execution_context=context.record() if snapshot.execution_context is not None else None)

    def execute(self, decision, snapshot, broker, journal, *, approved_orders=None, expected_account=None,
                recover=False, before_submit=None, enable_live=False):
        if broker.mode == "live" and (enable_live is not True or getattr(broker, "live_enabled", False) is not True):
            raise ValueError("Live execution requires explicit activation for this session")
        # Recompute before execution so a preview cannot be paired with other data.
        if decision != self.plan(snapshot):
            raise ValueError("Preview does not match the supplied market snapshot")
        journal.record_decision(decision)
        result = Executor(broker, journal, self.policy, self.fees,
                          settlement_seconds=10 if approved_orders is not None else 0).run(
            decision, snapshot.quotes, snapshot.execution_time,
            approved_orders=approved_orders, expected_account=expected_account,
            recover=recover, before_submit=before_submit)
        return dict(result, account=asdict(broker.account()))
