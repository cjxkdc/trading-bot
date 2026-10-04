"""One verified broker at a time; account verification never grants live trading."""
from dataclasses import dataclass
import time

from app.control_contracts import ControlError


@dataclass
class BrokerGrant:
    broker_id: str
    binding: str
    expires_at: float


class BrokerSession:
    def __init__(self, clock=time.monotonic, seconds=14400):
        self.clock, self.seconds = clock, seconds
        self.grant = None

    def clear(self):
        self.grant = None

    def verify(self, broker_id, binding):
        self.grant = BrokerGrant(broker_id, binding, self.clock() + self.seconds)

    def valid(self, broker_id, binding):
        grant = self.grant
        return bool(grant and grant.broker_id == broker_id and grant.binding == binding
                    and grant.expires_at > self.clock())

    def require(self, broker_id, binding):
        if not self.valid(broker_id, binding):
            raise ControlError('当前券商尚未核验，或账户连接已改变；请先检查当前模拟账户。')
