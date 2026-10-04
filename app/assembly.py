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

ROOT = Path(__file__).resolve().parents[1]


def strategy_catalog(directory=None):
    return _catalog(directory or ROOT / "strategies/plugins", "strategies", {"single", "portfolio"})


def platform_catalog(directory=None):
    # Compatibility aggregate; maintained manifests belong to their own modules.
    if directory is not None:
        return _catalog(directory, ("platforms", "data", "brokers"),
                        {"historical_data", "snapshot_data", "snapshot_collector", "broker", "account_reader", "market_reader"})
    data, brokers = data_catalog(), broker_catalog()
    if data.keys() & brokers.keys():
        raise ValueError("Duplicate data/broker plug-in names")
    return dict(data, **brokers)


def data_catalog(directory=None):
    return _catalog(directory or ROOT / "data/plugins", "data",
                    {"historical_data", "snapshot_data", "snapshot_collector", "market_reader"})


def broker_catalog(directory=None):
    return _catalog(directory or ROOT / "brokers/plugins", "brokers", {"broker", "account_reader"})


def _catalog(directory, namespace, kinds):
    from app.catalog import scan_catalog
    return scan_catalog(directory, namespace, kinds)


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


def strategy_data_requirements(config):
    from dataclasses import replace
    from framework.data_requirements import declared_requirements
    entry = strategy_catalog()[config['type']]
    strategy = build_portfolio_strategy(config)
    spec = portfolio_spec(config)
    request = declared_requirements(entry.get('data_requirements'), entry['input_schema'],
                                    strategy.history_requirements, spec['parameters'])
    factory = getattr(strategy, 'data_collection_policy', None)
    if factory is not None:
        signature = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        request = replace(request, collection_policy=factory(signature))
    return request


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


def build_snapshot_collector(config):
    return _build_platform(config, 'snapshot_collector')


def collection_config(root, source):
    """Resolve one data plug-in independently of the selected broker/strategy."""
    root = Path(root).resolve()
    raw = json.loads((root / 'configs/data_sources.json').read_text(encoding='utf-8-sig'))
    if source not in raw['sources']:
        raise ValueError('Unknown data source')
    config = raw['sources'][source]
    if set(config) != {'type', 'config_dir', 'source_config', 'cache_dir'}:
        raise ValueError('Invalid data source configuration')
    resolved = {'type': config['type']}
    for key in ('config_dir', 'source_config', 'cache_dir'):
        path = (root / config[key]).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Data source paths must stay in the project')
        resolved[key] = str(path)
    return resolved


def inspect_broker(name, config_dir, *, connect=False):
    entry = platform_catalog()[name]
    kind = entry['kind']
    if kind != 'account_reader' and not (kind == 'broker' and entry.get('account_status') is True):
        raise ValueError('This platform does not expose account diagnostics')
    reader = _build_platform({"type": name}, kind, config_dir=config_dir)
    return reader.inspect(connect=connect)


def inspect_market_data(name, config_dir=None, *, connect=False, symbol='AAPL', claim_device=False):
    if config_dir is None:
        config_dir = ROOT / platform_catalog()[name]['default_config_dir']
    reader = _build_platform({'type': name}, 'market_reader', config_dir=config_dir)
    return reader.inspect(connect=connect, symbol=symbol, claim_device=claim_device)


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
    return normalize_config(load_config(ROOT / "configs/portfolio_config.json"))


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
    with _open_execution_session(connector, cfg, snapshot, state_dir, fees) as session:
        yield session


@contextmanager
def open_broker_paper_session(cfg, snapshot, state_dir, fees):
    connector = _build_platform(cfg['broker'], 'broker')
    if connector.mode != 'broker_paper':
        raise ValueError('This command requires a broker-hosted paper account')
    state_dir = Path(state_dir).resolve() if state_dir else connector.state_dir
    # Validate the canonical location before creating directories or ledgers.
    if state_dir != connector.state_dir:
        raise ValueError('Use the configured broker paper state directory')
    with _open_execution_session(connector, cfg, snapshot, state_dir, fees) as session:
        yield session


@contextmanager
def open_live_session(cfg, snapshot, state_dir, fees, *, enable_live=False):
    if enable_live is not True:
        raise ValueError('Live execution requires --enable-live')
    connector = _build_platform(cfg['broker'], 'broker')
    if connector.mode != 'live':
        raise ValueError('The live command requires a live broker configuration')
    state_dir = Path(state_dir).resolve() if state_dir else connector.state_dir
    if state_dir != connector.state_dir:
        raise ValueError('Use the configured live state directory')
    with _open_execution_session(connector, cfg, snapshot, state_dir, fees,
                                 connect_options={'enable_live': True}) as session:
        yield session


@contextmanager
def _open_execution_session(connector, cfg, snapshot, state_dir, fees, *, connect_options=None):
    state_dir = Path(state_dir)
    names = (*connector.state_files, "journal.sqlite")
    if len(set(names)) != len(names) or any(
            Path(name).name != name or name in {"", ".", ".."} for name in names):
        raise ValueError("Adapter state files must be unique plain file names")
    if state_dir.exists():
        if not all((state_dir / name).is_file() for name in names):
            raise ValueError("Incomplete account state; never recreate a missing ledger/account")
    else:
        state_dir.mkdir(parents=True, exist_ok=False)
        try:
            connector.initialize(state_dir, cfg["initial_cash"])
        except Exception:
            # Failed login can precede any persisted state. Remove only our empty reservation;
            # partial/existing ledgers must survive for inspection, never be reset automatically.
            try:
                state_dir.rmdir()
            except OSError:
                pass
            raise
        Journal(state_dir / "journal.sqlite").close()
    broker = connector.connect(state_dir, snapshot, fees, **(connect_options or {}))
    try:
        journal = Journal(state_dir / "journal.sqlite")
        try:
            yield broker, journal
        finally:
            journal.close()
    finally:
        broker.close()


_open_simulation_session = _open_execution_session  # Compatibility for existing local integrations.


def write_demo_snapshot(path):
    from data.snapshot import demo_snapshot
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(demo_snapshot(), handle, indent=2)
