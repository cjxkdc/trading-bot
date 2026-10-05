"""Build a single paste-ready QC file from strategy, contracts and backtest adapter."""

from pathlib import Path
import ast
import hashlib
import json
import sys
import argparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.assembly import build_portfolio_runner, default_portfolio_config, portfolio_spec, strategy_catalog
from framework.configuration import load_config, normalize_config


def backtest_configuration(config):
    # Never put data-provider or broker connection parameters into a cloud export.
    cfg = normalize_config(config)
    if "backtest" not in cfg:
        raise ValueError("A portfolio backtest configuration is required")
    if float(cfg["fees"].get("bps", 0)) != 0:
        raise ValueError("This QC adapter supports fixed per-order fees only")
    from framework.scheduling import schedule_config
    cfg.setdefault('schedule', schedule_config({'initial_entry': 'scheduled'}))
    cfg.setdefault('benchmark', 'QQQ')
    return {k: v for k, v in cfg.items() if k not in {"data", "broker"}}


def build(config=None):
    config = backtest_configuration(config if config is not None else default_portfolio_config())
    runner = build_portfolio_runner(config)
    if runner.input_schema not in {"us_technology_daily_v1", "us_equity_momentum_daily_v1"}:
        raise ValueError("QC export requires a supported daily US equity schema")
    selected = strategy_catalog()[config['strategy']['type']]
    qc_profile = selected.get('backtest', {}).get('quantconnect', {})
    runner.engine.strategy.history_requirements  # Enforce the history request contract before export.
    plugins = {k: v for k, v in strategy_catalog().items()
               if v["kind"] == "portfolio" and v.get("input_schema") == runner.input_schema}
    sources = ["framework/portfolio.py", "framework/decisions.py", "framework/collection.py", "framework/scheduling.py"]
    sources += sorted({v["entrypoint"].split(":")[0].replace(".", "/") + ".py" for v in plugins.values()})
    sources.append("backtests/quantconnect/adapter.py")
    parts = []
    available_modules = {p[:-3].replace("/", ".") for p in sources} | {"app.assembly"}
    for name in sources:
        source = (ROOT / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        remove = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"framework", "strategies", "app"}:
                if node.module not in available_modules or node.level:
                    raise ValueError(f"Unbundled local dependency in {name}: {node.module}")
                remove.update(range(node.lineno, node.end_lineno + 1))
            elif isinstance(node, ast.ImportFrom) and node.level:
                raise ValueError(f"Relative imports need an explicit bundling rule: {name}")
            elif isinstance(node, ast.Import) and any(a.name.split('.')[0] in {'framework','strategies','app','platforms','data','brokers','backtests'} for a in node.names):
                raise ValueError(f"Use supported from-imports in paste-ready plugins: {name}")
        if name == "backtests/quantconnect/adapter.py":
            constructors = ", ".join(repr(k) + ": " + v["entrypoint"].split(":")[1] for k, v in plugins.items())
            specs = {k: portfolio_spec({"type": k}) for k in plugins}
            parts.append("# Frozen plug-in registry generated from local manifests.\n"
                + "_RUN_CONFIG = " + repr(config) + "\n"
                + "_INPUT_SCHEMA = " + repr(runner.input_schema) + "\n"
                + "_QC_PROFILE = " + repr(qc_profile) + "\n"
                + "_PLUGIN_CLASSES = {" + constructors + "}\n"
                + "_PLUGIN_SPECS = " + repr(specs) + "\n"
                + "def build_portfolio_strategy(config):\n"
                + "    return _PLUGIN_CLASSES[config['type']](**{k:v for k,v in config.items() if k not in {'type','id'}})\n"
                + "def portfolio_spec(config):\n"
                + "    base = _PLUGIN_SPECS[config['type']]\n"
                + "    params = dict(base['parameters'])\n"
                + "    params.update({k:v for k,v in config.items() if k not in {'type','id'}})\n"
                + "    return dict(base, parameters=params)\n")
        body = "\n".join(line for n, line in enumerate(source.splitlines(), 1) if n not in remove)
        parts.append(f"# ===== {name} =====\n{body}\n")
    body = "\n".join(parts)
    fingerprint = hashlib.sha256(body.encode()).hexdigest()
    result = ("# Paste this ENTIRE file into a new QuantConnect Python project's main.py.\n"
              "# Generated file: edit the original modules, then rebuild.\n"
              f"# Source SHA256: {fingerprint}\n\n" + body)
    ast.parse(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/portfolio_config.json")
    parser.add_argument("--out", type=Path, default=ROOT / "platforms/quantconnect/main.py")
    args = parser.parse_args()
    path = args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(load_config(args.config)), encoding="utf-8")
    print(path)
