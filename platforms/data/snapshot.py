"""Explicit file replay input. Provider must supply PIT universe + completed bars."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from framework.portfolio import PortfolioContext, StockSnapshot
from framework.runtime import MarketSnapshot
from framework.trading import Quote, amount

SCHEMA = "us_technology_daily_v1"


class SnapshotFile:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        stocks, quotes, now = _parse_snapshot(data)
        raw = data.get("portfolio_context")
        if raw is not None and (not isinstance(raw, dict) or set(raw) != {"as_of", "held_asset_ids"}
                                or not isinstance(raw["held_asset_ids"], list)):
            raise ValueError("Invalid portfolio context in snapshot")
        context = None if raw is None else PortfolioContext(
            date.fromisoformat(raw["as_of"]), tuple(raw["held_asset_ids"]))
        return MarketSnapshot(SCHEMA, stocks, quotes, now, context)


def load_snapshot(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return _parse_snapshot(data)


def _parse_snapshot(data):
    if data["input_schema"] != SCHEMA or data.get("universe_complete") is not True:
        raise ValueError("Unsupported snapshot schema")
    now = datetime.fromisoformat(data["execution_time"])
    if now.tzinfo is None:
        raise ValueError("Execution timestamp must include timezone")
    stocks = tuple(StockSnapshot(
        s["asset_id"], s["ticker"], date.fromisoformat(s["known_on"]),
        date.fromisoformat(s["price_as_of"]), s["industry"], float(s["operating_income_ttm"]),
        tuple(map(float, s["adjusted_closes"])), tuple(map(float, s["dollar_volumes"])))
        for s in data["stocks"])
    quotes = {}
    for q in data["quotes"]:
        if q["asset_id"] in quotes:
            raise ValueError("Duplicate quote")
        quotes[q["asset_id"]] = Quote(q["asset_id"], amount(q["bid"], True),
            amount(q["ask"], True), datetime.fromisoformat(q["as_of"]))
    return stocks, quotes, now


def demo_snapshot():
    # Synthetic data for plumbing tests only. No market-return claims.
    stamp = "2026-09-01T09:35:00-04:00"
    stocks, quotes = [], []
    for i in range(8):
        sid = f"DEMO-{i:02d}"
        prices = [100.0] * 253
        prices[-22] = 100 + i * 5
        stocks.append({"asset_id": sid, "ticker": sid, "known_on": "2026-08-28",
                       "price_as_of": "2026-08-31", "industry": str(i // 2),
                       "operating_income_ttm": 1000000, "adjusted_closes": prices,
                       "dollar_volumes": [20000000.0] * 20})
        quotes.append({"asset_id": sid, "bid": "99.90", "ask": "100.10", "as_of": stamp})
    return {"input_schema": SCHEMA, "universe_complete": True, "synthetic": True, "execution_time": stamp,
            "stocks": stocks, "quotes": quotes}
