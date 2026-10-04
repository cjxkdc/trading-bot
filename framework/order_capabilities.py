"""Execution compatibility describes orders, never a strategy or research schema."""
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class OrderRequirements:
    market: str
    asset_class: str
    currency: str
    order_type: str
    quantity_type: str
    time_in_force: str

    def record(self):
        return asdict(self)


US_EQUITY_LIMIT_ORDERS = OrderRequirements('US', 'equity', 'USD', 'limit', 'whole', 'day')


def order_capability_issues(capabilities, requirements):
    if not isinstance(requirements, OrderRequirements):
        return ['数据接入未声明证券与订单要求，无法准备或执行订单。']
    if not isinstance(capabilities, dict):
        return ['券商模块尚未声明可执行的订单能力。']
    labels = {'market': '市场', 'asset_class': '资产类别', 'currency': '币种',
              'order_type': '订单类型', 'quantity_type': '股数类型', 'time_in_force': '有效期'}
    issues = []
    for field, value in requirements.record().items():
        allowed = capabilities.get(field)
        if not isinstance(allowed, list) or not value or value not in allowed:
            issues.append(f'券商模块不支持所需{labels[field]}：{value}。')
    return issues
