"""Compatibility import for the application task runner."""
import sys
from app import task_client as _implementation
sys.modules[__name__] = _implementation
