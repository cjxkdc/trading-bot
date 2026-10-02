"""Explicit moomoo AU real-account adapter; never selected by paper commands."""
from platforms.brokers.opend_broker import OpenDConnector


class MoomooLiveConnector(OpenDConnector):
    provider = 'moomoo'
    mode = 'live'
    environment = 'REAL'
