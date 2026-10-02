"""moomoo AU simulation profile."""
from platforms.brokers.opend_paper import OpenDPaperConnector


class MoomooPaperConnector(OpenDPaperConnector):
    provider = 'moomoo'
