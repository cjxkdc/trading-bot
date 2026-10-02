"""Futu HK simulation profile."""
from platforms.brokers.opend_paper import OpenDPaperConnector


class FutuPaperConnector(OpenDPaperConnector):
    provider = 'futu'
