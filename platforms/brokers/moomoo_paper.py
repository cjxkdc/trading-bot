"""moomoo AU simulation profile."""
from platforms.brokers.opend_broker import OpenDConnector


class MoomooPaperConnector(OpenDConnector):
    provider = 'moomoo'
