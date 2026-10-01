"""Composition root. Only this layer loads concrete, trusted local plug-ins."""
import importlib
import json
import re
import hashlib
import inspect
from contextlib import contextmanager
from pathlib import Path
from framework.configuration import load_config, normalize_config, policy_from_config, fees_from_config
from framework.journal import Journal
from framework.portfolio import PortfolioEngine
from framework.runtime import PortfolioRunner

ROOT = Path(__file__).resolve().parent


def strategy_catalog(directory=None):
    return _catalog(directory or ROOT / "strategies/plugins", "strategies", {"single", "portfolio"})


def platform_catalog(directory=None):
    return _catalog(directory or ROOT / "platforms/plugins", "platforms",
                    {"historical_data", "snapshot_data", "broker", "account_reader"})


def _catalog(directory, namespace, kinds):
    catalog = {}
    for path in sorted(Path(directory).glob("*.json")):
        entry = json.loads(path.read_text(encoding="utf-8"))
        name = entry["type"]
        module, cls = entry["entrypoint"].split(":")
        if (not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in catalog
                or not re.fullmatch(re.escape(namespace) + r"\.[a-zA-Z_][\w.]*", module)
                or not cls.isidentifier() or entry["kind"] not in kinds
                or not entry.get("version")):
            raise ValueError(f"Invalid or duplicate {namespace} manifest: {path.name}")
        catalog[name] = entry
    return catalog


def _build(config, kind):
    try:
        entry = strategy_catalog()[config["type"]]
    except KeyError as exc:
        raise ValueError("Unknown strategy; add its local plug-in manifest first") from exc
    if entry["kind"] != kind:
        raise ValueError(f"Strategy requires the {entry['kind']} runner")
    module, name = entry["entrypoint"].split(":")
    params = {k: v for k, v in config.items() if k not in {"id", "type"}}
    return getattr(importlib.import_module(module), name)(**params)


def build_strategy(config):
    return _build(config, "single")


def build_portfolio_strategy(config):
    return _build(config, "portfolio")


def portfolio_spec(config):
    entry = strategy_catalog()[config["type"]]
    module, cls = entry["entrypoint"].split(":")
    source = (ROOT / (module.replace(".", "/") + ".py")).read_bytes()
    constructor = getattr(importlib.import_module(module), cls)
    parameters = inspect.signature(constructor).bind(
        **{k: v for k, v in config.items() if k not in {"type", "id"}})
    parameters.apply_defaults()
    return {"type": entry["type"], "version": entry["version"],
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "engine_sha256": hashlib.sha256(b"".join((ROOT / p).read_bytes() for p in
                ("framework/portfolio.py", "framework/decisions.py"))).hexdigest(),
            "parameters": dict(parameters.arguments)}


def build_data_provider(name, path):
    return _build_platform({"type": name}, "historical_data", path=path)


def inspect_broker(name, config_dir, *, connect=False):
    reader = _build_platform({"type": name}, "account_reader", config_dir=config_dir)
    return reader.inspect(connect=connect)


def _build_platform(config, kind, **inputs):
    try:
        entry = platform_catalog()[config["type"]]
    except KeyError as exc:
        raise ValueError("Unknown platform; add its trusted local manifest first") from exc
    if entry["kind"] != kind:
        raise ValueError(f"Platform requires the {entry['kind']} interface")
    module, name = entry["entrypoint"].split(":")
    parameters = {k: v for k, v in config.items() if k != "type"}
    if set(parameters) & set(inputs):
        raise ValueError("Adapter configuration duplicates a runtime input")
    return getattr(importlib.import_module(module), name)(**parameters, **inputs)


def default_portfolio_config():
    return normalize_config(load_config(ROOT / "portfolio_config.json"))


def prepare_portfolio(config_path, snapshot_path):
    cfg = normalize_config(load_config(config_path))
    runner = build_portfolio_runner(cfg)
    data_entry = platform_catalog()[cfg["data"]["type"]]
    if data_entry.get("input_schema") != runner.input_schema:
        raise ValueError("Strategy and data input schemas do not match")
    provider = _build_platform(cfg["data"], "snapshot_data", path=snapshot_path)
    snapshot = provider.load()
    return cfg, runner, snapshot, runner.plan(snapshot)


def build_portfolio_runner(cfg):
    cfg = normalize_config(cfg)
    policy, fees = policy_from_config(cfg), fees_from_config(cfg)
    entry = strategy_catalog()[cfg["strategy"]["type"]]
    strategy = build_portfolio_strategy(cfg["strategy"])
    engine = PortfolioEngine(strategy, float(policy.max_gross), float(policy.max_weight))
    return PortfolioRunner(engine, portfolio_spec(cfg["strategy"]), entry["input_schema"], policy, fees)


@contextmanager
def open_paper_session(cfg, snapshot, state_dir, fees):
    connector = _build_platform(cfg["broker"], "broker")
    if connector.mode != "paper":
        raise ValueError("This release supports paper connections only")
    state_dir = Path(state_dir)
    names = (*connector.state_files, "journal.sqlite")
    if len(set(names)) != len(names) or any(
            Path(name).name != name or name in {"", ".", ".."} for name in names):
        raise ValueError("Adapter state files must be unique plain file names")
    if state_dir.exists():
        if not all((state_dir / name).is_file() for name in names):
            raise ValueError("Incomplete paper state; never recreate a missing ledger/account")
    else:
        state_dir.mkdir(parents=True, exist_ok=False)
        connector.initialize(state_dir, cfg["initial_cash"])
        Journal(state_dir / "journal.sqlite").close()
    broker = connector.connect(state_dir, snapshot, fees)
    try:
        journal = Journal(state_dir / "journal.sqlite")
        try:
            yield broker, journal
        finally:
            journal.close()
    finally:
        broker.close()


def write_demo_snapshot(path):
    from platforms.data.snapshot import demo_snapshot
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(demo_snapshot(), handle, indent=2)
