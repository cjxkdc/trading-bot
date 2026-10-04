"""Shared Tiger connection-file diagnostics; no order or market clients."""
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
SDK_VERSION = "3.8.0"
CONFIG_FILE = "tiger_openapi_config.properties"
GATEWAYS = {"https://openapi.tigerfintech.com/gateway", "https://openapi.tradeup.com/gateway"}

def _masked(account):
    return "***" + str(account)[-4:] if len(str(account)) > 4 else "***"

def configuration_check(config_dir):
    """No SDK client construction, key generation, login, or network access."""
    path = Path(config_dir).resolve() / CONFIG_FILE
    issues = []
    try:
        sdk = version("tigeropen")
    except PackageNotFoundError:
        sdk = None
    if sdk != SDK_VERSION:
        issues.append("Install requirements/tiger.txt (supported SDK: " + SDK_VERSION + ")")
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
