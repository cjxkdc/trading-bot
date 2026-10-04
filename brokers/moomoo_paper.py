"""moomoo AU simulation profile."""
from brokers.opend_broker import OpenDConnector


class MoomooPaperConnector(OpenDConnector):
    provider = 'moomoo'
