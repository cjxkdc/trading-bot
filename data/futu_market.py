"""Futu market-data diagnostics."""
from data.opend_market import OpenDMarketData


class FutuMarketData(OpenDMarketData):
    provider = 'futu'
