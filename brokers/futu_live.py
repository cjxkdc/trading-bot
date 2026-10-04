"""Explicit Futu HK real-account adapter; never selected by paper commands."""
from brokers.opend_broker import OpenDConnector


class FutuLiveConnector(OpenDConnector):
    provider = 'futu'
    mode = 'live'
    environment = 'REAL'
