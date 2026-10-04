"""moomoo market-data diagnostics."""
from data.opend_market import OpenDMarketData


class MoomooMarketData(OpenDMarketData):
    provider = 'moomoo'
