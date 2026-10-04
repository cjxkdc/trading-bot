"""Compatibility import; maintained implementation: brokers.moomoo_live."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("brokers.moomoo_live")
