"""Futu HK simulation profile."""
from brokers.opend_broker import OpenDConnector


class FutuPaperConnector(OpenDConnector):
    provider = 'futu'
