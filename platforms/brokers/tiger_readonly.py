"""Compatibility import; maintained implementation: brokers.tiger_readonly."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("brokers.tiger_readonly")
