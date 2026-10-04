"""Tiger account inspection. Deliberately not an ExecutionBroker."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import logging
from pathlib import Path

from platforms.tiger import SDK_VERSION, CONFIG_FILE, GATEWAYS, configuration_check, _masked
READ_METHODS = frozenset({"accounts", "assets", "positions", "active_orders"})


class TigerReadOnlyError(RuntimeError):
    """A sanitized error; never include SDK exception text or raw responses."""


def _scalar(value):
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return str(number) if number.is_finite() else None


def _fields(obj, names):
    return {name: _scalar(getattr(obj, name, None)) for name in names}


def _readonly_client_class():
    from tigeropen.trade.trade_client import TradeClient

    class ReadOnlyClient(TradeClient):
        def execute(self, request, url=None):
            # Enforce read-only at the SDK request boundary as well as our public
            # API. An accidental place/modify/cancel/transfer call fails locally.
            if getattr(request, "_method", None) not in READ_METHODS:
                raise TigerReadOnlyError("Non-read-only Tiger request blocked")
            if url is not None and url not in GATEWAYS:
                raise TigerReadOnlyError("Unexpected Tiger gateway blocked")
            return super().execute(request, url=url)
    return ReadOnlyClient


def _connect(config_dir):
    from tigeropen.tiger_open_config import TigerOpenClientConfig
    config = TigerOpenClientConfig(props_path=str(Path(config_dir).resolve()),
                                   sandbox_debug=False, enable_dynamic_domain=False)
    if config.server_url not in GATEWAYS or not config.account or not config.private_key or not config.tiger_id:
        raise TigerReadOnlyError("Invalid Tiger configuration")
    config.timeout = 10
    config.retry_max_tries = 1
    config.retry_max_time = 10
    config.token_refresh_duration = 0
    config.log_path = None
    logger = logging.Logger("trading_bot_tiger_readonly")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.disabled = True
    return _readonly_client_class()(config, logger=logger), str(config.account), config.is_paper


def account_snapshot(client, account, is_paper):
    """Allowlisted values only. Requests are sequential, not an atomic snapshot."""
    started = datetime.now(timezone.utc).isoformat()
    profiles = client.get_managed_accounts(account=account)
    if profiles is None:
        raise TigerReadOnlyError("Tiger did not return account profiles")
    profile = next((p for p in profiles if str(p.account) == account), None)
    if profile is None:
        raise TigerReadOnlyError("Configured account was not returned by Tiger")
    assets = client.get_assets(account=account)
    positions = client.get_positions(account=account, sec_type=None)
    orders = client.get_open_orders(account=account, sec_type=None)
    if assets is None or not assets or positions is None or orders is None:
        raise TigerReadOnlyError("Tiger returned an incomplete account response")
    if any(str(getattr(row, "account", "")) != account for row in [*assets, *positions, *orders]):
        raise TigerReadOnlyError("Tiger returned data for an unexpected account")
    money = [_fields(a.summary, ("currency", "cash", "net_liquidation", "available_funds", "buying_power", "timestamp"))
             for a in assets]
    holdings = [dict(_fields(p, ("quantity", "market_price", "market_value", "average_cost")),
                     contract=_fields(p.contract, ("symbol", "sec_type", "currency", "exchange", "expiry", "strike", "put_call")))
                for p in positions]
    open_orders = [dict(_fields(o, ("id", "action", "quantity", "filled", "status", "limit_price", "order_type", "update_time")),
                        contract=_fields(o.contract, ("symbol", "sec_type", "currency", "expiry", "strike", "put_call")))
                   for o in orders]
    return {"mode": "read_only", "status": "connected", "account": _masked(account),
            "account_environment": "tiger_paper" if is_paper else "real",
            "profile": _fields(profile, ("capability", "status", "account_type")),
            "assets": money, "positions": holdings, "open_orders": open_orders,
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "atomic_snapshot": False, "network_requested": True,
            "note": "Read-only account diagnostics; order-list completeness and market-data access are not certified"}


class TigerReadOnly:
    mode = "read_only"

    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)

    def inspect(self, *, connect=False):
        check = configuration_check(self.config_dir)
        if not connect:
            return check
        if check["issues"]:
            raise TigerReadOnlyError("Tiger configuration is not ready; run broker-status without --connect")
        try:
            client, account, is_paper = _connect(self.config_dir)
            return account_snapshot(client, account, is_paper)
        except TigerReadOnlyError:
            raise
        except Exception:
            # SDK errors can contain account IDs, tokens or signed request URLs.
            raise TigerReadOnlyError("Tiger read-only request failed; check API authorization, account access and connectivity") from None
