"""Futu market-data diagnostics."""
from platforms.data.opend_market import OpenDMarketData


class FutuMarketData(OpenDMarketData):
    provider = 'futu'
