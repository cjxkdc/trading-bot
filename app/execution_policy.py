"""Deployment execution policy, separate from account environments and adapter readiness.

Recognizing an environment does not certify a connector or authorize trading.
The current deployment permits local replay and broker paper accounts only.
There is deliberately no request, environment-variable, or UI override here;
live execution needs a separately completed and verified control path.
"""
from dataclasses import dataclass
from types import MappingProxyType

from app.control_contracts import ControlError


ACCOUNT_ENVIRONMENTS = MappingProxyType({
    'paper': '本地回放',
    'broker_paper': '模拟账户',
    'live': '实盘',
})


@dataclass(frozen=True)
class ExecutionPolicy:
    identifier: str
    allowed_environments: frozenset[str]

    def allows(self, environment):
        return (isinstance(environment, str) and environment in ACCOUNT_ENVIRONMENTS
                and environment in self.allowed_environments)

    def rejection_reason(self, environment):
        if not isinstance(environment, str) or environment not in ACCOUNT_ENVIRONMENTS:
            return '账户环境未登记，当前部署拒绝使用此模块。'
        if self.allows(environment):
            return None
        return f'当前部署尚未开放{ACCOUNT_ENVIRONMENTS[environment]}账户；此模块已登记，但不能在当前工作台使用。'

    def require(self, environment):
        reason = self.rejection_reason(environment)
        if reason:
            raise ControlError(reason)

    def public(self):
        # A fresh JSON-safe description, never an authorization accepted from a client.
        return {
            'known_environments': list(ACCOUNT_ENVIRONMENTS),
            'allowed_environments': [mode for mode in ACCOUNT_ENVIRONMENTS if self.allows(mode)],
            'live_enabled': self.allows('live'),
        }


CURRENT_EXECUTION_POLICY = ExecutionPolicy(
    identifier='simulation_only',
    allowed_environments=frozenset({'paper', 'broker_paper'}),
)
