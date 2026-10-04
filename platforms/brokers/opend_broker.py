"""Compatibility import; maintained implementation: brokers.opend_broker."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("brokers.opend_broker")
