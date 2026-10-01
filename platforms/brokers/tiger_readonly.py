"""Tiger account inspection. Deliberately not an ExecutionBroker."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
import logging
import os
from pathlib import Path

SDK_VERSION = "3.8.0"
CONFIG_FILE = "tiger_openapi_config.properties"
READ_METHODS = frozenset({"accounts", "assets", "positions", "active_orders"})
GATEWAYS = {"https://openapi.tigerfintech.com/gateway", "https://openapi.tradeup.com/gateway"}


class TigerReadOnlyError(RuntimeError):
    """A sanitized error; never include SDK exception text or raw responses."""


def _masked(account):
    return "***" + str(account)[-4:] if len(str(account)) > 4 else "***"


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


def configuration_check(config_dir):
    """No SDK client construction, key generation, login, or network access."""
    path = Path(config_dir).resolve() / CONFIG_FILE
    issues = []
    try:
        sdk = version("tigeropen")
    except PackageNotFoundError:
        sdk = None
    if sdk != SDK_VERSION:
        issues.append("Install requirements-tiger.txt (supported SDK: " + SDK_VERSION + ")")
    if not path.is_file():
        issues.append("Missing " + CONFIG_FILE + " in the explicitly selected directory")
    # SDK 3.8.0 gives environment variables precedence over props_path. Never
    # silently query a different account or send signed requests to an override.
    if any(k.startswith("TIGEROPEN_") and v for k, v in os.environ.items()):
        issues.append("TIGEROPEN_* environment overrides must be unset for this explicit-file adapter")
    account = None
    if path.is_file() and sdk == SDK_VERSION:
        try:
            from jproperties import Properties
            properties = Properties()
            with path.open("rb") as handle:
                properties.load(handle, "utf-8")
            values = {k: properties[k].data.strip() for k in properties}
            for key in ("account", "tiger_id", "license"):
                if not values.get(key):
                    issues.append("Missing configuration field: " + key)
            if not any(values.get(k) for k in ("private_key", "private_key_pk8", "private_key_pk1")):
                issues.append("Missing exported private key field")
            if values.get("env", "PROD").upper() != "PROD":
                issues.append("Use PROD gateway for both Tiger paper and real accounts")
            if values.get("server_url") and values["server_url"] not in GATEWAYS:
                issues.append("Custom server_url is not supported")
            account = values.get("account")
        except Exception:
            issues.append("Unable to parse the configuration file; export it again from Tiger OpenAPI")
    return {"mode": "read_only", "status": "configuration_present_not_authenticated" if not issues else "not_ready",
            "network_requested": False, "sdk_version": sdk,
            "account": _masked(account) if account else None, "issues": issues}


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
