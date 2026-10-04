"""Compatibility facade; workbench operations live in the application service."""
import sys
from app import workbench as _implementation
sys.modules[__name__] = _implementation
