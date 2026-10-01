"""Identical decision records for QC and local replay; no broker or data SDK."""
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from math import isfinite
from framework.portfolio import PortfolioEngine


def canonical(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and not isfinite(value):
        return {"nonfinite": str(value)}
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    decision_id: str
    targets: tuple
    record: dict


def decide(engine: PortfolioEngine, stocks, execution_day, strategy_spec,
           *, input_schema="us_technology_daily_v1", context=None):
    stocks = tuple(sorted(stocks, key=lambda s: s.asset_id))
    if len({s.price_as_of for s in stocks}) > 1:
        raise ValueError("All candidates must share the same completed signal session")
    targets = engine.plan(stocks, execution_day, context)
    record = {
        "schema": 1, "input_schema": input_schema,
        "execution_day": execution_day.isoformat(),
        "strategy": canonical(strategy_spec),
        "limits": {"max_gross": engine.max_gross, "max_weight": engine.max_weight},
        "data_sha256": digest([asdict(s) for s in stocks]),
        "targets": [asdict(t) for t in targets],
    }
    if context is not None:
        record["portfolio_context"] = canonical(asdict(context))
    return Decision(digest(record), targets, record)
