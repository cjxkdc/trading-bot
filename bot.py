"""Select plug-ins, preview a portfolio, replay locally, or export a QC backtest."""
import argparse
import sys
import json
from pathlib import Path
from app.assembly import (inspect_broker, inspect_market_data, open_paper_session, open_broker_paper_session, open_live_session, platform_catalog, prepare_portfolio,
                      strategy_catalog, write_demo_snapshot)
from framework.configuration import load_config, normalize_config
from framework.journal import packed
from platforms.opend import OpenDError

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "configs/portfolio_config.json"
# Compatibility export for explicit integrations; never an implicit CLI selection.
TIGER_PAPER_CONFIG = ROOT / 'configs/tiger_paper_config.json'


def run_paper(snapshot_path, config_path, state_dir):
    cfg, runner, snapshot, decision = prepare_portfolio(config_path, snapshot_path)
    with open_paper_session(cfg, snapshot, state_dir, runner.fees) as (broker, journal):
        return dict(runner.execute(decision, snapshot, broker, journal), mode="local_paper_replay")


def run_broker_paper(snapshot_path, config_path, state_dir=None):
    cfg, runner, snapshot, decision = prepare_portfolio(config_path, snapshot_path)
    with open_broker_paper_session(cfg, snapshot, state_dir, runner.fees) as (broker, journal):
        return dict(runner.execute(decision, snapshot, broker, journal), mode='broker_paper')


def run_live(snapshot_path, config_path, state_dir=None, *, enable_live=False, unlock=False):
    if enable_live is not True:
        raise ValueError('Live execution requires --enable-live')
    cfg, runner, snapshot, decision = prepare_portfolio(config_path, snapshot_path)
    with open_live_session(cfg, snapshot, state_dir, runner.fees, enable_live=True) as (broker, journal):
        if unlock:
            import getpass
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                try:
                    password = getpass.getpass('OpenD trade password (not stored): ')
                except (getpass.GetPassWarning, EOFError):
                    raise OpenDError('Secure password input is unavailable; use an interactive terminal or unlock in OpenD GUI') from None
            try:
                broker.unlock(password)
            finally:
                del password
        return dict(runner.execute(decision, snapshot, broker, journal), mode='live')


def export_backtest(config_path, out, *, start=None, end=None, slippage_bps=None, note="", schedule=None):
    from tools.build_quantconnect import backtest_configuration, build
    from hashlib import sha256
    cfg = normalize_config(load_config(config_path))
    if schedule is not None:
        cfg['schedule'] = schedule
    for key, value in (("start", start), ("end", end), ("slippage_bps", slippage_bps)):
        if value is not None:
            cfg["backtest"][key] = value
    cfg = normalize_config(cfg)
    source = build(cfg)
    manifest = {"schema_version": 1, "status": "exported_not_run",
                "configuration": backtest_configuration(cfg),
                "main_sha256": sha256(source.encode("utf-8")).hexdigest(),
                "research_note": note,
                "execution_model": {'backtest': 'LEAN market orders with configured fee/slippage models',
                                    'runtime': 'broker DAY limit orders',
                                    'data': 'QC universe/fundamentals may differ from the runtime provider',
                                    'initial_clock': 'Backtest starts at midnight; immediate entry uses the first available session'}}
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    (out / "main.py").write_text(source, encoding="utf-8", newline="\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"status": "exported_not_run", "main": str(out / "main.py"),
            "manifest": str(out / "manifest.json")}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'workbench':
        from app.workbench_cli import main as workbench_main
        return workbench_main(sys.argv[2:])
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
    trade = sub.add_parser('broker-paper', help='One simulation execution pass; pending orders pause the batch')
    trade.add_argument('--snapshot', type=Path, required=True, help='Current real-market snapshot; never synthetic demo data')
    trade.add_argument('--config', type=Path, required=True, help='Legacy explicit profile; everyday trading uses: workbench execute')
    trade.add_argument('--state', type=Path, help='Must match the broker configuration state_dir')
    live = sub.add_parser('broker-live', help='Real-account execution; explicit configuration and activation required')
    live.add_argument('--snapshot', type=Path, required=True)
    live.add_argument('--config', type=Path, required=True, help='A *_live_config.json; no live default')
    live.add_argument('--state', type=Path, help='Must match the configured live state directory')
    live.add_argument('--enable-live', action='store_true', help='Explicitly enable real order transmission for this invocation')
    live.add_argument('--unlock', action='store_true', help='Command-line OpenD only: prompt for trade password; GUI users unlock in OpenD')
    export = sub.add_parser("export-qc", help="Export code to paste into QuantConnect; does not run it")
    export.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    export.add_argument("--out", type=Path, required=True, help="New directory for this experiment")
    export.add_argument("--start", help="Override historical start date, YYYY-MM-DD")
    export.add_argument("--end", help="Override historical end date, YYYY-MM-DD")
    export.add_argument("--slippage-bps", type=float, help="Override one-way slippage for a cost stress test")
    export.add_argument("--note", default="", help="Record the hypothesis before viewing results")
    ingest = sub.add_parser("import-qc", help="Archive an exported QC JSON report with experiment evidence")
    ingest.add_argument("--experiment", type=Path, required=True)
    ingest.add_argument("--result", type=Path, required=True)
    ingest.add_argument("--code", type=Path, help="main.py downloaded from that QC run's Code tab")
    compare = sub.add_parser("compare-qc", help="Compare imported reports; no automatic strategy approval")
    compare.add_argument("--results", type=Path, nargs="+", required=True)
    compare.add_argument("--out", type=Path, required=True)
    broker = sub.add_parser("broker-status", help="Offline check by default; --connect sends read-only queries")
    broker.add_argument("--broker", help='Explicit legacy diagnostic plugin; omit all profile options to use the selected workbench broker')
    broker.add_argument('--config', type=Path)
    broker.add_argument("--config-dir", type=Path)
    broker.add_argument("--connect", action="store_true")
    market = sub.add_parser('market-status', help='Data-access diagnostic; never produces or executes orders')
    market.add_argument('--provider')
    market.add_argument('--config-dir', type=Path, help='Default: selected provider configuration directory')
    market.add_argument('--symbol', default='AAPL', help='Probe only this symbol to limit history-quota usage')
    market.add_argument('--connect', action='store_true')
    market.add_argument('--claim-device', action='store_true',
                        help='Explicitly claim quote access; may disconnect another API device')
    market.add_argument('--out', type=Path, help='New report directory; required with --connect')
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
        print(json.dumps(export_backtest(args.config, args.out, start=args.start, end=args.end,
                                        slippage_bps=args.slippage_bps, note=args.note)))
    elif args.command == "import-qc":
        from backtests.experiments import import_result
        from backtests.quantconnect.results import parse_result
        path = import_result(args.experiment, args.result, parse_result, run_code=args.code)
        print(json.dumps({"result_dir": str(path)}))
    elif args.command == "compare-qc":
        from backtests.experiments import compare_results
        from backtests.quantconnect.results import parse_result
        report = compare_results(args.results, parse_result, args.out)
        print(json.dumps({"report": str(args.out / "comparison.md"),
                          "comparison_differences": report["comparison_differences"]}))
    elif args.command == "broker-status":
        if args.config is None and args.broker is None and args.config_dir is None:
            from app.workbench_cli import main as workbench_main
            return workbench_main(['status' if args.connect else 'offline'])
        if args.config is None:
            parser.error('Legacy diagnostics require an explicit --config; use workbench status for the current account')
        cfg = normalize_config(load_config(args.config))
        name = args.broker or cfg['broker']['type']
        config_dir = args.config_dir
        if config_dir is None:
            if name != cfg['broker']['type']:
                parser.error('An explicit --config-dir is required when overriding --broker')
            config_dir = ROOT / cfg['broker']['config_dir']
        try:
            result = inspect_broker(name, config_dir, connect=args.connect)
        except OpenDError as exc:
            result = {'status': 'failed', 'issues': [str(exc)], 'network_requested': args.connect}
        print(json.dumps(result, indent=2))
        if result.get("issues"):
            parser.exit(2)
    elif args.command == 'market-status':
        if args.provider is None and args.out is None and not args.claim_device:
            from app.workbench_cli import main as workbench_main
            return workbench_main(['data-status' if args.connect else 'view'])
        from data.tiger_market import TigerMarketError
        if args.claim_device and not args.connect:
            parser.error('--claim-device requires --connect')
        if args.connect and args.out is None:
            parser.error('--connect requires a new --out directory to preserve the diagnostic')
        if args.out is not None:
            args.out.mkdir(parents=True, exist_ok=False)
        if args.provider is None:
            parser.error('Quote diagnostics require --provider; use workbench data-status for the selected data source')
        try:
            result = inspect_market_data(args.provider, args.config_dir, connect=args.connect,
                                         symbol=args.symbol, claim_device=args.claim_device)
        except (TigerMarketError, OpenDError) as exc:
            result = {'status': 'failed', 'reason': str(exc), 'strategy_ready': False}
        if args.out is not None:
            report_path = args.out / 'market-status.json'
            report_path.write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
            # Keep full history in the report, not the terminal.
            display = {k: v for k, v in result.items() if k != 'checks'}
            display['report'] = str(report_path)
            display['checks'] = {k: {'status': v['status'], 'rows': len(v.get('data', []))}
                                 for k, v in result.get('checks', {}).items()}
        else:
            display = result
        print(json.dumps(display, indent=2))
        if result.get('issues') or result.get('status') == 'failed':
            parser.exit(2)
    elif args.command in {'broker-paper', 'broker-live'}:
        from framework.execution import ExecutionBlocked
        from brokers.tiger_paper import TigerPaperError
        try:
            if args.command == 'broker-live':
                if not args.enable_live:
                    parser.error('broker-live requires --enable-live; no connection or order was attempted')
                result = run_live(args.snapshot, args.config, args.state, enable_live=True, unlock=args.unlock)
            else:
                result = run_broker_paper(args.snapshot, args.config, args.state)
            print(packed(result))
        except (ExecutionBlocked, TigerPaperError, OpenDError, ValueError) as exc:
            print(json.dumps({'status': 'paused', 'reason': str(exc), 'automatic_retry': False}))
            parser.exit(2)
    else:
        print(packed(run_paper(args.snapshot, args.config, args.state)))


if __name__ == "__main__":
    raise SystemExit(main())
