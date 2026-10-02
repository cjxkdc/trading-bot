"""moomoo market-data diagnostics."""
from platforms.data.opend_market import OpenDMarketData


class MoomooMarketData(OpenDMarketData):
    provider = 'moomoo'
