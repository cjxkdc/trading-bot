"""Compatibility import; maintained implementation: brokers.futu_live."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("brokers.futu_live")
