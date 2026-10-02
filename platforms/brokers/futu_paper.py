"""Futu HK simulation profile."""
from platforms.brokers.opend_broker import OpenDConnector


class FutuPaperConnector(OpenDConnector):
    provider = 'futu'
