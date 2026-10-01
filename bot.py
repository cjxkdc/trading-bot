"""Select plug-ins, preview a portfolio, replay locally, or export a QC backtest."""
import argparse
import json
from pathlib import Path
from assembly import (open_paper_session, platform_catalog, prepare_portfolio,
                      strategy_catalog, write_demo_snapshot)
from framework.configuration import load_config, normalize_config
from framework.journal import packed

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "portfolio_config.json"


def run_paper(snapshot_path, config_path, state_dir):
    cfg, runner, snapshot, decision = prepare_portfolio(config_path, snapshot_path)
    with open_paper_session(cfg, snapshot, state_dir, runner.fees) as (broker, journal):
        return dict(runner.execute(decision, snapshot, broker, journal), mode="local_paper_replay")


def export_backtest(config_path, out):
    from tools.build_quantconnect import backtest_configuration, build
    from hashlib import sha256
    cfg = normalize_config(load_config(config_path))
    source = build(cfg)
    manifest = {"schema_version": 1, "status": "exported_not_run",
                "configuration": backtest_configuration(cfg),
                "main_sha256": sha256(source.encode("utf-8")).hexdigest()}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / "main.py").write_text(source, encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"status": "exported_not_run", "main": str(out / "main.py"),
            "manifest": str(out / "manifest.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("strategies")
    sub.add_parser("platforms")
    show = sub.add_parser("config", help="Show the fully resolved portfolio configuration")
    show.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    demo = sub.add_parser("demo")
    demo.add_argument("--out", type=Path, required=True, help="New directory; synthetic data only")
    demo.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    plan = sub.add_parser("plan", help="Preview targets without creating an account or sending orders")
    plan.add_argument("--snapshot", type=Path, required=True)
    plan.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    paper = sub.add_parser("paper")
    paper.add_argument("--snapshot", type=Path, required=True)
    paper.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    paper.add_argument("--state", type=Path, required=True)
    export = sub.add_parser("export-qc", help="Export code to paste into QuantConnect; does not run it")
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    export.add_argument("--out", type=Path, required=True, help="New directory for this experiment")
    args = parser.parse_args()
    if args.command in {"strategies", "platforms"}:
        catalog = strategy_catalog() if args.command == "strategies" else platform_catalog()
        print(json.dumps(list(catalog.values()), ensure_ascii=False, indent=2))
    elif args.command == "config":
        print(json.dumps(normalize_config(load_config(args.config)), ensure_ascii=False, indent=2))
    elif args.command == "demo":
        args.out.mkdir(parents=True, exist_ok=False)
        path = args.out / "synthetic-snapshot.json"
        write_demo_snapshot(path)
        result = run_paper(path, args.config, args.out / "state")
        print(packed(dict(result, synthetic=True)))
    elif args.command == "plan":
        _, _, _, decision = prepare_portfolio(args.config, args.snapshot)
        print(json.dumps(decision.record, ensure_ascii=False, indent=2))
    elif args.command == "export-qc":
        print(json.dumps(export_backtest(args.config, args.out)))
    else:
        print(packed(run_paper(args.snapshot, args.config, args.state)))


if __name__ == "__main__":
    main()
