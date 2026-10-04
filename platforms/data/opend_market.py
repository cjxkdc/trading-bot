"""Compatibility import; maintained implementation: data.opend_market."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("data.opend_market")
