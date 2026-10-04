"""Compatibility import; maintained implementation: backtests.engine."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("backtests.engine")
