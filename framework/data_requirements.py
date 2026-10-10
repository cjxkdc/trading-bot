"""A strategy's data request, independent of any vendor or broker."""
from dataclasses import dataclass, field, fields as dataclass_fields
from hashlib import sha256
import json
import re
from framework.collection import RankedCollectionPolicy


@dataclass(frozen=True)
class DataRequirements:
    input_schema: str
    universe: str
    fields: tuple[str, ...]
    price_bars: int
    volume_bars: int
    market: str = 'us'  # Legacy manifest compatibility; new modules declare their own market.
    security_type: str = 'common_stock'
    frequency: str = 'daily'
    collection_policy: RankedCollectionPolicy | None = field(default=None, repr=False)

    def __post_init__(self):
        for value in (self.input_schema, self.universe, self.market, self.security_type, self.frequency):
            if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', value):
                raise ValueError('Invalid data requirement identifier')
        if (not isinstance(self.fields, tuple) or not self.fields
                or any(not isinstance(f, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', f) for f in self.fields)
                or len(set(self.fields)) != len(self.fields)):
            raise ValueError('Data fields must be unique identifiers')
        if any(type(n) is not int or n < 1 for n in (self.price_bars, self.volume_bars)):
            raise ValueError('Data history must be positive')
        object.__setattr__(self, 'fields', tuple(sorted(self.fields)))
        if self.collection_policy is not None and not isinstance(self.collection_policy, RankedCollectionPolicy):
            raise ValueError('Invalid strategy collection policy')

    def record(self):
        result = {f.name: getattr(self, f.name) for f in dataclass_fields(self) if f.name != 'collection_policy'}
        result['fields'] = list(self.fields)
        if self.collection_policy is not None:
            result['collection_policy'] = self.collection_policy.record()
        return result

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.record(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def describe(self):
        names = {'adjusted_closes': '复权收盘价', 'industry': '行业',
                 'dollar_volumes': '成交额', 'operating_income_ttm': 'TTM 营业利润',
                 'market_cap': '市值', 'raw_close': '未复权股价', 'share_volumes': '成交股数'}
        return dict(self.record(), fingerprint=self.fingerprint,
                    universe_label=({'technology': '美国科技股', 'all': '美国普通股（全行业）'}.get(self.universe, self.universe)
                                    if self.market == 'us' else f'{self.market.upper()} · {self.universe}'),
                    field_labels=[names.get(name, name) for name in self.fields])


def declared_requirements(definition, input_schema, history, parameters):
    """Manifests own the scope/fields; constructor parameters own history/conditions."""
    if not isinstance(definition, dict) or set(definition) - {
            'universe', 'fields', 'conditional_fields', 'market', 'security_type', 'frequency'}:
        raise ValueError('Strategy has not declared valid data requirements')
    if not isinstance(definition.get('fields'), list):
        raise ValueError('Strategy fields must be a list')
    fields = list(definition['fields'])
    for rule in definition.get('conditional_fields', []):
        if (not isinstance(rule, dict) or set(rule) != {'parameter', 'equals', 'fields'}
                or rule['parameter'] not in parameters or not isinstance(rule['fields'], list)):
            raise ValueError('Invalid conditional data requirement')
        value = parameters[rule['parameter']]
        if type(value) is type(rule['equals']) and value == rule['equals']:
            fields.extend(rule['fields'])
    return DataRequirements(input_schema, definition['universe'], tuple(fields),
                            history.price_bars, history.dollar_volume_bars,
                            definition.get('market', 'us'), definition.get('security_type', 'common_stock'),
                            definition.get('frequency', 'daily'))
