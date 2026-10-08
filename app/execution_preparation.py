"""Canonical preparation API; legacy import path remains for persisted integrations."""
from app.paper_preparation import (SCHEMA, LEGACY_SCHEMA, prepare, execute_prepared,
    market_snapshot, mapping_for, execution_context, signal_for, require_orders)
