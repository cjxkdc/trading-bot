"""QuantConnect adapter; bundle with tools/build_quantconnect.py before upload."""

from AlgorithmImports import *
from datetime import datetime, timedelta
import json
from math import isfinite
from framework.portfolio import StockSnapshot, PortfolioEngine, PortfolioContext
from framework.decisions import decide, digest
from assembly import build_portfolio_strategy, portfolio_spec, default_portfolio_config


# The exporter freezes the reviewed config here; local tests load the same config.
if "_RUN_CONFIG" not in globals():
    _RUN_CONFIG = {k: v for k, v in default_portfolio_config().items()
                   if k not in {"data", "broker"}}
START = tuple(map(int, _RUN_CONFIG["backtest"]["start"].split("-")))
END = tuple(map(int, _RUN_CONFIG["backtest"]["end"].split("-")))
INITIAL_CASH = float(_RUN_CONFIG["initial_cash"])
SLIPPAGE_BPS = float(_RUN_CONFIG["backtest"]["slippage_bps"])
FEE_PER_ORDER = float(_RUN_CONFIG["fees"]["per_order"])
MODE = "configured"  # Legacy momentum/profit/full overrides remain explicit opt-ins.
VERSION = "0.4.0"
STRATEGY_TYPE = _RUN_CONFIG["strategy"]["type"]
STRATEGY_PARAMETERS = {k: v for k, v in _RUN_CONFIG["strategy"].items() if k not in {"type", "id"}}


def strategy_configuration():
    # When the exported type is switched manually, do not pass another plug-in's parameters.
    params = dict(STRATEGY_PARAMETERS) if STRATEGY_TYPE == _RUN_CONFIG["strategy"]["type"] else {}
    if STRATEGY_TYPE == "tech_profit_momentum" and MODE != "configured":
        params.update(require_profit=MODE != "momentum",
                      industry_limit=2 if MODE == "full" else params.get("count", 5))
    return {"type": STRATEGY_TYPE, **params}


def make_decision(engine, candidates, execution_day, context=None):
    return decide(engine, candidates, execution_day, portfolio_spec(strategy_configuration()), context=context)


class TechProfitMomentumAlgorithm(QCAlgorithm):
    def initialize(self):
        if self.live_mode:
            raise RuntimeError("Research backtest only; live execution is not implemented")
        if MODE not in ("configured", "momentum", "profit", "full"):
            raise ValueError("Unknown MODE")
        if float(_RUN_CONFIG["fees"].get("bps", 0)) != 0:
            raise ValueError("This QC adapter supports fixed per-order fees only")
        self.set_start_date(*START)
        self.set_end_date(*END)
        self.set_cash(INITIAL_CASH)
        self.set_time_zone(TimeZones.NEW_YORK)
        self.settings.daily_precise_end_time = True
        self.settings.free_portfolio_value_percentage = 0
        self.universe_settings.resolution = Resolution.MINUTE
        self.universe_settings.data_normalization_mode = DataNormalizationMode.RAW
        self.universe_settings.leverage = 1
        self.universe_settings.fill_forward = False
        self.universe_settings.asynchronous = False
        self.add_security_initializer(self._configure_security)
        self.qqq = self.add_equity(
            "QQQ", Resolution.MINUTE,
            data_normalization_mode=DataNormalizationMode.ADJUSTED
        ).symbol
        self.set_benchmark(self.qqq)
        self._hours = self.securities[self.qqq].exchange.hours
        self._engine = PortfolioEngine(build_portfolio_strategy(strategy_configuration()),
            float(_RUN_CONFIG["limits"]["max_gross"]), float(_RUN_CONFIG["limits"]["max_weight"]))
        self._fact_snapshots = {}
        self._last_month = None
        self._pending = None
        self._selected_symbols = []
        self._completed = 0
        self.add_universe(self._select_universe)
        # Only to obtain prior fundamental snapshots, not to trade.
        # Each monthly signal separately requests the strategy's declared price history.
        self.set_warm_up(timedelta(days=10), Resolution.DAILY)

    def _configure_security(self, security):
        security.set_leverage(1)
        security.set_fee_model(ConstantFeeModel(FEE_PER_ORDER, "USD"))
        security.set_slippage_model(ConstantSlippageModel(SLIPPAGE_BPS / 10_000))

    def _freeze_facts(self, fundamentals):
        facts = {}
        for f in fundamentals:
            if (not f.has_fundamental_data
                    or f.asset_classification.morningstar_sector_code != MorningstarSectorCode.TECHNOLOGY):
                continue
            ref = f.security_reference
            if (ref.security_type != "ST00000001" or ref.is_depositary_receipt
                    or not ref.is_primary_share):
                continue
            # Copy scalar values now. Fundamental objects have time-dependent getters.
            income = float(f.financial_statements.income_statement.operating_income.twelve_months)
            industry = int(f.asset_classification.morningstar_industry_code)
            if industry <= 0:
                continue
            sid = str(f.symbol.id)
            facts[sid] = (f.symbol, str(industry), income)
        return facts

    def _select_universe(self, fundamentals):
        # Data arrivals need not occur on an exchange session. In particular,
        # Friday's data may arrive on Saturday. Never discard those snapshots.
        self._fact_snapshots[self.time] = self._freeze_facts(fundamentals)
        oldest = self.time - timedelta(days=14)
        self._fact_snapshots = {stamp: facts for stamp, facts in self._fact_snapshots.items()
                                if stamp >= oldest}
        # A weekend callback can prepare Monday's basket. Do not depend on a
        # callback arriving on the first trading morning itself.
        execution_day = self.time.date()
        while not self._is_session(execution_day):
            execution_day += timedelta(days=1)
        # Preparing a basket at the warmup boundary is safe; on_data still
        # forbids orders during warmup. Otherwise a holiday start can miss January.
        if execution_day < datetime(*START).date():
            return Universe.UNCHANGED
        month = (execution_day.year, execution_day.month)
        if month == self._last_month:
            return Universe.UNCHANGED
        signal_day = self._previous_session(execution_day)
        # A mid-month start stays cash until the next complete month-end signal.
        if (signal_day.year, signal_day.month) == month:
            return Universe.UNCHANGED
        snapshot_at, prior_facts = self._facts_for_signal(signal_day)
        candidates = self._load_candidates(prior_facts, signal_day, snapshot_at.date())
        context = None
        if getattr(self._engine.strategy, "requires_context", False) is True:
            context = PortfolioContext(self.time.date(), tuple(
                str(kv.key.id) for kv in self.portfolio if kv.value.invested))
        decision = make_decision(self._engine, candidates, execution_day, context)
        targets = decision.targets
        by_id = {stock.asset_id: stock for stock in candidates}
        symbols = {sid: facts[0] for sid, facts in prior_facts.items()}
        self._selected_symbols = [symbols[t.asset_id] for t in targets]
        self._pending = (execution_day, targets, symbols)
        self._last_month = month
        audit = {
            "version": VERSION, "execution_day": str(execution_day),
            "decision_id": decision.decision_id, "strategy": decision.record["strategy"],
            "signal_day": str(signal_day), "fundamentals_known_at": snapshot_at.isoformat(),
            "mode": MODE, "complete_histories": len(candidates),
            "selected": [{"ticker": by_id[t.asset_id].ticker, "weight": t.weight,
                          "momentum": round(t.score, 6),
                          "industry": by_id[t.asset_id].industry,
                          "operating_income_ttm": (by_id[t.asset_id].operating_income_ttm
                              if isfinite(by_id[t.asset_id].operating_income_ttm) else None)}
                         for t in targets]
        }
        self.log("SELECTION " + json.dumps(audit, allow_nan=False))
        return self._selected_symbols

    def _is_session(self, day):
        return self._hours.is_date_open(datetime.combine(day, datetime.min.time()))

    def _previous_session(self, day):
        day -= timedelta(days=1)
        while not self._is_session(day):
            day -= timedelta(days=1)
        return day

    def _facts_for_signal(self, signal_day):
        # Preserve the original conservative policy: only information received
        # by midnight at the start of the month-end session. For a Monday
        # month-end, Saturday's delivery is valid; a Monday callback is optional.
        cutoff = datetime.combine(signal_day, datetime.min.time())
        earliest = datetime.combine(self._previous_session(signal_day) + timedelta(days=1),
                                    datetime.min.time())
        eligible = [stamp for stamp in self._fact_snapshots if earliest <= stamp <= cutoff]
        if not eligible:
            cached = ", ".join(stamp.isoformat() for stamp in sorted(self._fact_snapshots))
            raise RuntimeError(f"Missing timely fundamentals: required {earliest} through {cutoff}; cached [{cached}]")
        stamp = max(eligible)
        facts = self._fact_snapshots[stamp]
        if not facts:
            raise RuntimeError(f"Empty technology fundamental snapshot at {stamp}; check data coverage")
        return stamp, facts

    @staticmethod
    def _daily_rows(history, symbol, cutoff):
        if history.empty:
            return {}
        try:
            frame = history.loc[symbol].sort_index()
        except KeyError:
            return {}
        rows = {}
        for stamp, row in frame.iterrows():
            end = stamp.to_pydatetime()
            if end > cutoff:
                raise ValueError("Future history bar")
            # Supports both precise daily close stamps and midnight end stamps.
            day = (end - timedelta(microseconds=1)).date() if end.hour == end.minute == 0 else end.date()
            if day in rows:
                raise ValueError("Duplicate daily bar")
            rows[day] = (float(row["close"]), float(row["volume"]))
        return rows

    def _history(self, symbols, count, normalization):
        return self.history(
            symbols, count, Resolution.DAILY, fill_forward=False,
            extended_market_hours=False, data_normalization_mode=normalization
        )

    def _load_candidates(self, facts, prior_day, known_on=None):
        requirements = build_portfolio_strategy(strategy_configuration()).history_requirements
        history_count = max(requirements.price_bars, requirements.dollar_volume_bars)
        reference = self._daily_rows(
            self._history([self.qqq], history_count, DataNormalizationMode.ADJUSTED), self.qqq, self.time
        )
        sessions = sorted(reference)
        if len(sessions) != history_count or sessions[-1] != prior_day:
            raise RuntimeError(f"Expected {history_count} completed daily bars through previous session")
        # Selection rules (including profitability) belong to the strategy.
        # All point-in-time technology candidates go through the same data contract.
        pool = sorted(facts.items())
        result = []
        for offset in range(0, len(pool), 40):
            batch = pool[offset:offset + 40]
            symbols = [f[0] for _, f in batch]
            adjusted = self._history(symbols, requirements.price_bars, DataNormalizationMode.ADJUSTED)
            raw = self._history(symbols, requirements.dollar_volume_bars, DataNormalizationMode.RAW)
            for sid, (symbol, industry, income) in batch:
                prices = self._daily_rows(adjusted, symbol, self.time)
                volumes = self._daily_rows(raw, symbol, self.time)
                # An IPO, halt or missing session cannot shorten the lookback silently.
                price_days = sessions[-requirements.price_bars:]
                volume_days = sessions[-requirements.dollar_volume_bars:]
                if not all(day in prices for day in price_days) or not all(day in volumes for day in volume_days):
                    continue
                result.append(StockSnapshot(
                    sid, symbol.value, known_on or prior_day, sessions[-1], industry, income,
                    tuple(prices[day][0] for day in price_days),
                    tuple(volumes[day][0] * volumes[day][1] for day in volume_days)
                ))
        return result

    def _fresh(self, data, symbol):
        if not data.bars.contains_key(symbol):
            return False
        bar = data.bars[symbol]
        return (not bar.is_fill_forward and bar.end_time == self.time
                and bar.close > 0 and self.securities[symbol].is_tradable)

    def on_data(self, data):
        if self.is_warming_up or self._pending is None:
            return
        today, targets, symbols = self._pending
        if self.time.date() < today:
            return
        if self.time.date() > today:
            raise RuntimeError("Rebalance missed its execution session")
        if (self.time.hour, self.time.minute) < (9, 35):
            return
        held = [kv.key for kv in self.portfolio if kv.value.invested]
        missing_held = [s for s in held if not self._fresh(data, s)]
        missing_new = [symbols[t.asset_id] for t in targets if not self._fresh(data, symbols[t.asset_id])]
        if missing_held or missing_new:
            if (self.time.hour, self.time.minute) < (10, 0):
                return
            if missing_held:
                raise RuntimeError("Cannot safely rebalance stale or halted existing holdings")
            self.log("UNFILLED cash slots: " + ",".join(s.value for s in missing_new))
        executable = [PortfolioTarget(symbols[t.asset_id], t.weight) for t in targets
                      if self._fresh(data, symbols[t.asset_id])]
        self._pending = None
        tag = "tech-profit-momentum " + str(today)
        if executable:
            # LEAN processes reductions before increases, including reweighting
            # retained names. Synchronous market orders; RAW portfolio accounting.
            self.set_holdings(executable, liquidate_existing_holdings=True, tag=tag)
        else:
            self.liquidate(tag=tag)
        self._completed += 1
        self.plot("Portfolio", "Target stock count", len(executable))
        self.log("REBALANCED " + str(today) + " targets=" + str(len(executable)))

    def on_order_event(self, event):
        if event.status == OrderStatus.INVALID:
            raise RuntimeError("Rejected backtest order: " + str(event))

    def on_end_of_algorithm(self):
        self.set_runtime_statistic("Version", VERSION)
        # Encoded hex values can be omitted from QC runtime statistics. Keep
        # the fingerprint in logs and human-readable run settings in the banner.
        self.log("RESEARCH_CONFIG " + json.dumps({"sha256": digest(_RUN_CONFIG), "configuration": _RUN_CONFIG}))
        self.set_runtime_statistic("Research start", datetime(*START).date().isoformat())
        self.set_runtime_statistic("Research end", datetime(*END).date().isoformat())
        self.set_runtime_statistic("Monthly rebalances", str(self._completed))
        self.set_runtime_statistic("Strategy mode", MODE)
        self.set_runtime_statistic("Costs", f"${FEE_PER_ORDER}/order + {SLIPPAGE_BPS}bps slippage")
