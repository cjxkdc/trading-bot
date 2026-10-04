"""Compatibility import for the application broker grant."""
import sys
from app import broker_session as _implementation
sys.modules[__name__] = _implementation
