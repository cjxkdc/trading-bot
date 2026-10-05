"""Load a portable portfolio configuration, without importing any plug-ins."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from framework.trading import ExecutionPolicy, Fees, amount


def load_config(path, _seen=()):
    """An optional extends path is relative to its own file; top-level blocks replace."""
    path = Path(path).resolve()
    if path in _seen:
        raise ValueError("Configuration inheritance cycle")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    parent = value.pop("extends", None)
    if parent is not None:
        if not isinstance(parent, str):
            raise ValueError("extends must be a local file path")
        inherited = load_config(path.parent / parent, (*_seen, path))
        inherited.update(value)
        return inherited
    return value


def normalize_config(value):
    cfg = deepcopy(value)
    if not isinstance(cfg, dict):
        raise ValueError("Configuration must be an object")
    allowed = {"schema_version", "strategy", "data", "broker", "limits", "fees",
               "initial_cash", "backtest", "schedule", "benchmark"}
    if set(cfg) - allowed:
        raise ValueError("Unknown configuration fields: " + ", ".join(sorted(set(cfg) - allowed)))
    # Compatibility with v0.2 paper_config.json. These defaults select local replay only.
    cfg.setdefault("schema_version", 1)
    cfg.setdefault("data", {"type": "snapshot_file"})
    cfg.setdefault("broker", {"type": "paper"})
    if type(cfg["schema_version"]) is not int or cfg["schema_version"] != 1:
        raise ValueError("Unsupported portfolio configuration version")
    for field in ("strategy", "data", "broker"):
        if not isinstance(cfg.get(field), dict) or not isinstance(cfg[field].get("type"), str):
            raise ValueError(f"{field} must name a registered plug-in type")
    policy_from_config(cfg)
    fees_from_config(cfg)
    cfg["initial_cash"] = str(amount(cfg["initial_cash"], True))
    if 'schedule' in cfg:
        from framework.scheduling import schedule_config
        cfg['schedule'] = schedule_config(cfg['schedule'])
    if 'benchmark' in cfg:
        import re
        if not isinstance(cfg['benchmark'], str) or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', cfg['benchmark']):
            raise ValueError('Invalid benchmark ticker')
    if "backtest" in cfg:
        settings = cfg["backtest"]
        if not isinstance(settings, dict) or set(settings) != {"backend", "start", "end", "slippage_bps"}:
            raise ValueError("backtest requires backend, start, end and slippage_bps")
        if settings["backend"] != "quantconnect":
            raise ValueError("The portfolio backtest exporter supports quantconnect only")
        start, end = date.fromisoformat(settings["start"]), date.fromisoformat(settings["end"])
        if start >= end:
            raise ValueError("Backtest start must precede end")
        if amount(settings["slippage_bps"]) >= 10000:
            raise ValueError("Invalid slippage rate")
    return cfg


def policy_from_config(cfg):
    values = dict(cfg["limits"])
    for key in ("max_gross", "max_weight", "max_order_value"):
        values[key] = amount(values[key], True)
    return ExecutionPolicy(**values)


def fees_from_config(cfg):
    return Fees(**{key: amount(value) for key, value in cfg["fees"].items()})
