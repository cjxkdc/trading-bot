"""Offline single-asset research. No network or brokerage dependencies."""
import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from app.assembly import build_strategy, build_data_provider
from backtests.engine import backtest as run_backtest
from backtests.benchmarks import BuyAndHoldBenchmark


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_data(path):
    return build_data_provider("csv", path).load()


def signal(prices, end, strategy):
    # Compatibility for the first-version research API.
    return build_strategy(strategy).target(tuple((None, p) for p in prices[:end+1]))


def backtest(rows, strategy, *args, **kwargs):
    return run_backtest(rows, build_strategy(strategy), *args, **kwargs)


def code_fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "app/research.py", root / "app/assembly.py",
             root / "backtests/engine.py", root / "backtests/benchmarks.py"]
    for folder in ("framework", "strategies", "platforms"):
        paths.extend(sorted((root / folder).rglob("*.py")))
    paths.extend(sorted((root / "strategies/plugins").glob("*.json")))
    paths.extend(sorted((root / "platforms/plugins").glob("*.json")))
    content = [(p.relative_to(root).as_posix(), fingerprint(p)) for p in paths]
    return hashlib.sha256(json.dumps(content).encode()).hexdigest()


def evaluate(rows, strategy, cfg, period, multiplier=1):
    return backtest(rows, strategy, cfg["periods"][period], cfg["initial_capital"],
                    cfg["max_weight"], cfg["cost_bps_one_way"] * multiplier,
                    cfg["minimum_sessions"])


def evaluate_benchmark(rows, cfg, period, multiplier=1):
    return run_backtest(rows, BuyAndHoldBenchmark(), cfg["periods"][period], cfg["initial_capital"],
                        cfg["max_weight"], cfg["cost_bps_one_way"] * multiplier,
                        cfg["minimum_sessions"])


def passes(result, benchmark, gates):
    return (result["cagr"] >= gates["min_cagr"] and
            result["max_drawdown"] <= gates["max_drawdown"] and
            result["cagr"] - benchmark["cagr"] >= gates["min_excess_cagr"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["screen", "validate", "holdout"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="Research configuration for your registered single-asset plugins")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    periods = [list(map(date.fromisoformat, cfg["periods"][p]))
               for p in ["development", "validation", "holdout"]]
    if any(a >= b for a, b in periods) or any(a[1] >= b[0] for a, b in zip(periods, periods[1:])):
        raise ValueError("Periods must be ordered and non-overlapping")
    if not (0 < cfg["max_weight"] <= 1 and cfg["initial_capital"] > 0
            and 0 <= cfg["cost_bps_one_way"] < 10000):
        raise ValueError("Invalid capital, weight or cost")
    ids = [s["id"] for s in cfg["candidates"]]
    if not ids or len(set(ids)) != len(ids) or not 1 <= cfg["shortlist_size"] <= 2:
        raise ValueError("Unique candidates and shortlist of one or two required")
    rows = build_data_provider(cfg.get("data_provider", "csv"), args.data).load()
    provenance = {"data": fingerprint(args.data), "config": fingerprint(args.config),
                  "engine": code_fingerprint()}
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / (args.stage + ".json")
    if target.exists():
        raise ValueError("Stage already recorded; reports cannot be overwritten")
    candidates = cfg["candidates"]
    previous_hash = None
    if args.stage != "screen":
        previous_path = args.out / ("screen.json" if args.stage == "validate" else "validate.json")
        previous = json.loads(previous_path.read_text())
        if previous["provenance"] != provenance:
            raise ValueError("Frozen data/config/engine changed")
        previous_hash = fingerprint(previous_path)
        chosen = previous["shortlist"] if args.stage == "validate" else previous["passed"]
        candidates = [s for s in candidates if s["id"] in chosen]
        if not candidates:
            raise ValueError("No candidate passed the previous stage")
    period = {"screen": "development", "validate": "validation", "holdout": "holdout"}[args.stage]
    benchmark = evaluate_benchmark(rows, cfg, period)
    results = {s["id"]: evaluate(rows, s, cfg, period) for s in candidates}
    report = {"stage": args.stage, "provenance": provenance, "previous_report_sha256": previous_hash,
              "benchmark": benchmark, "results": results, "live_authorized": False}
    if args.stage == "screen":
        eligible = [k for k, v in results.items() if passes(v, benchmark, cfg["gates"])]
        report["shortlist"] = sorted(eligible, key=lambda k: (-results[k]["cagr"], k))[:cfg["shortlist_size"]]
    else:
        report["passed"] = [k for k, v in results.items() if passes(v, benchmark, cfg["gates"])]
    if args.stage == "holdout":
        stress_benchmark = evaluate_benchmark(rows, cfg, period, 2)
        stress = {s["id"]: evaluate(rows, s, cfg, period, 2) for s in candidates}
        report.update(stress=stress, stress_benchmark=stress_benchmark)
        report["passed"] = [k for k in report["passed"] if passes(stress[k], stress_benchmark, cfg["gates"])]
        report["next_step"] = "execution_model_review_then_paper" if report["passed"] else "reject_candidates"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"report": str(target), "shortlist": report.get("shortlist"),
                      "passed": report.get("passed"), "live_authorized": False}))


if __name__ == "__main__":
    main()
