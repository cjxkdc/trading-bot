"""Compatibility import; the shared credential store belongs to the application."""
import sys
from app import credential_store as _implementation
sys.modules[__name__] = _implementation
