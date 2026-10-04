"""Compatibility import; maintained implementation: data.futu_technology."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("data.futu_technology")
