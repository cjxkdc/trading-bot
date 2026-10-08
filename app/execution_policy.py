"""Account-scoped execution permission; adapter support is independent of permission.

The platform recognizes local replay, broker simulation and live accounts. Live
order transmission additionally needs a server-side grant pinned to the exact
bound account identity. Reading accounts and reconciling existing orders remain
available without a transmission grant. No request or browser value is a grant.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from types import MappingProxyType

from app.control_contracts import ControlError

ACCOUNT_ENVIRONMENTS = MappingProxyType({
    'paper': '本地回放', 'broker_paper': '模拟账户', 'live': '实盘',
})

@dataclass(frozen=True)
class ExecutionPolicy:
    identifier: str = 'account_scoped'
    allowed_environments: frozenset[str] = frozenset(ACCOUNT_ENVIRONMENTS)
    live_accounts: tuple[tuple[str, str], ...] = ()

    def allows(self, environment):
        return (isinstance(environment, str) and environment in ACCOUNT_ENVIRONMENTS
                and environment in self.allowed_environments)

    def rejection_reason(self, environment):
        if not self.allows(environment):
            return '账户环境未登记，当前部署拒绝使用此模块。'
        return None

    def require(self, environment):
        reason = self.rejection_reason(environment)
        if reason:
            raise ControlError(reason)

    def execution_rejection(self, environment, profile_id, account_ref=None):
        reason = self.rejection_reason(environment)
        if reason or environment != 'live':
            return reason
        if (not isinstance(account_ref, str) or not re.fullmatch(r'[0-9a-f]{64}', account_ref)
                or dict(self.live_accounts).get(profile_id) != account_ref):
            return '此实盘账户尚未获得服务器执行授权；账户查看与订单核对仍可使用。'
        return None

    def require_execution(self, environment, profile_id, account_ref=None):
        reason = self.execution_rejection(environment, profile_id, account_ref)
        if reason:
            raise ControlError(reason)

    def public(self):
        return {'known_environments': list(ACCOUNT_ENVIRONMENTS),
                'allowed_environments': [mode for mode in ACCOUNT_ENVIRONMENTS if self.allows(mode)],
                'live_enabled': bool(self.live_accounts),
                'live_profiles': [profile for profile, _ in self.live_accounts]}

CURRENT_EXECUTION_POLICY = ExecutionPolicy()

def load_execution_policy(root):
    """Read only a server-owned policy file; malformed grants fail closed."""
    path = Path(root) / 'configs/execution_policy.json'
    if not path.exists():
        return CURRENT_EXECUTION_POLICY
    try:
        if path.stat().st_size > 65536:
            raise ValueError('size')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(value, dict) or set(value) != {'version', 'live_accounts'}
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(value['live_accounts'], dict)):
            raise ValueError('schema')
        grants = []
        for profile, grant in value['live_accounts'].items():
            if (not isinstance(profile, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', profile)
                    or not isinstance(grant, dict) or set(grant) != {'account_ref'}
                    or not isinstance(grant['account_ref'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', grant['account_ref'])):
                raise ValueError('grant')
            grants.append((profile, grant['account_ref']))
        return ExecutionPolicy(live_accounts=tuple(sorted(grants)))
    except (OSError, ValueError, TypeError, KeyError):
        raise ControlError('服务器执行授权配置无效；未启用实盘交易。') from None
