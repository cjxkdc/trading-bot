"""Broker authorization lifecycle independent of HTTP routes and UI components."""
from app.control_contracts import ControlError
from app.credential_store import ActiveVault
from app.module_registry import load_class


class BrokerAuthorization:
    def __init__(self, registry):
        self.registry = registry
        self.current = None

    def provider(self, broker_id):
        spec = self.registry.descriptor('broker', broker_id).get('authentication', {})
        if spec.get('mode') != 'oauth':
            raise ControlError('此券商通过官方客户端或 API 配置登录，不提供工作台网页授权。')
        if self.current is None:
            cls = load_class(spec['entrypoint'], 'brokers')
            self.current = cls(self.registry.root, broker_id)
        return self.current

    def disconnect(self):
        if self.current is not None:
            self.current.disconnect()
        # Also discard a persisted grant after restart, before a provider was instantiated.
        ActiveVault(self.registry.root).clear()
        self.current = None
