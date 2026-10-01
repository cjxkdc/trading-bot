"""Read QC JSON downloads/API responses. No guessed returns or completion flags."""
from datetime import date
from decimal import Decimal, InvalidOperation
import math


def _get(obj, key, default=None):
    if not isinstance(obj, dict):
        return default
    matches = [v for k, v in obj.items() if k.casefold() == key.casefold()]
    if len(matches) > 1:
        raise ValueError("Ambiguous QC field: " + key)
    return matches[0] if matches else default


def _number(value, percent=False):
    if value is None or value in ("", "N/A", "--"):
        return None
    # QC display percentages must carry units; 0.14 without '%' is ambiguous.
    if isinstance(value, bool) or (percent and not (isinstance(value, str) and value.strip().endswith("%"))):
        raise ValueError("QC percentage is missing explicit units" if percent else "Invalid QC number")
    if not percent and isinstance(value, str) and "%" in value:
        raise ValueError("Unexpected percentage units in QC statistic")
    text = str(value).strip().removesuffix("%").replace(",", "")
    try:
        result = float(Decimal(text))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("Invalid QC statistic") from exc
    if not math.isfinite(result):
        raise ValueError("Non-finite QC statistic")
    return result / 100 if percent else result


def _day(value):
    if value is None:
        return None
    return date.fromisoformat(str(value)[:10]).isoformat()


def parse_result(payload):
    if not isinstance(payload, dict) or _get(payload, "success") is False:
        raise ValueError("QC response is not a successful results document")
    outer = _get(payload, "backtest", payload)
    result = _get(outer, "results", outer)
    stats = _get(result, "statistics")
    runtime = _get(result, "runtimeStatistics", {})
    if not isinstance(stats, dict) or not stats or not isinstance(runtime, dict):
        raise ValueError("QC JSON must contain non-empty Statistics; PDF/CSV are not supported")
    status = str(_get(outer, "status", _get(payload, "status", ""))).strip()
    completed = _get(outer, "completed", _get(payload, "completed"))
    progress = _number(_get(outer, "progress", _get(payload, "progress")))
    if completed is not None and type(completed) is not bool:
        raise ValueError("Invalid QC completed flag")
    if progress is not None and not 0 <= progress <= 1:
        raise ValueError("Invalid QC progress")
    errors = any(_get(obj, "error") or _get(obj, "runtimeError") or
                 _get(obj, "hasInitializeError") for obj in (payload, outer, result))
    state = status.casefold().rstrip(".")
    if errors or "error" in state or "fail" in state:
        completion = "failed"
    elif completed is False or (progress is not None and progress < 1) or state in {"in progress", "in queue", "stopped", "cancelled", "canceled"}:
        completion = "incomplete"
    elif completed is True or state == "completed":
        completion = "reported_complete"
    else:
        # A populated Statistics table (or progress=1 alone) is not proof of a
        # clean finish. Some downloads omit job metadata altogether.
        completion = "unverified"
    total_return = _get(stats, "Net Profit", _get(runtime, "Return"))
    metrics = {
        "total_return": _number(total_return, True),
        "annual_return": _number(_get(stats, "Compounding Annual Return"), True),
        "max_drawdown": _number(_get(stats, "Drawdown"), True),
        "sharpe": _number(_get(stats, "Sharpe Ratio")),
        "total_orders": _number(_get(stats, "Total Orders")),
    }
    if all(v is None for v in metrics.values()):
        raise ValueError("QC JSON contains no recognized performance statistics")
    if metrics["total_orders"] is not None and (metrics["total_orders"] < 0 or not metrics["total_orders"].is_integer()):
        raise ValueError("Invalid QC order count")
    if metrics["max_drawdown"] is not None and not 0 <= metrics["max_drawdown"] <= 1:
        raise ValueError("Invalid QC drawdown")
    # PeriodStart/Finish are LEAN packet dates. Do not interpret created or
    # request/finish wall-clock timestamps as the historical testing interval.
    period = {}
    for key, field in (("start", "periodStart"), ("end", "periodFinish")):
        packet_date, declared_date = _day(_get(outer, field)), _day(_get(runtime, "Research " + key))
        if packet_date and declared_date and packet_date != declared_date:
            raise ValueError("Conflicting historical dates in QC result")
        period[key] = packet_date or declared_date
    if all(period.values()) and period["start"] > period["end"]:
        raise ValueError("Invalid QC historical period")
    return {"provider": "quantconnect", "completion": completion,
            "provider_status": status, "progress": progress,
            "backtest_id": _get(outer, "backtestId"), "period": period,
            "metrics": metrics}
