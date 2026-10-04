"""Optional, pure strategy hooks for exact ranked-prefix data collection.

Providers supply histories and hydrate candidates in priority order. Only the
strategy decides eligibility, priority and when lower-ranked candidates cannot
change its selection. No vendor, network, account or order handles belong here.
"""
from dataclasses import dataclass, field
from datetime import date
import re
from typing import Callable


@dataclass(frozen=True)
class PriceHistory:
    ticker: str
    price_as_of: date
    adjusted_closes: tuple[float, ...]
    raw_close: float
    share_volumes: tuple[float, ...]
    dollar_volumes: tuple[float, ...] = ()


@dataclass(frozen=True)
class RankedCollectionPolicy:
    signature: str
    priority: Callable = field(repr=False, compare=False)
    complete: Callable = field(repr=False, compare=False)

    def __post_init__(self):
        if (not isinstance(self.signature, str) or not re.fullmatch(r'[0-9a-f]{64}', self.signature)
                or not callable(self.priority) or not callable(self.complete)):
            raise ValueError('Invalid ranked collection policy')

    def record(self):
        return {'kind': 'ranked_prefix_v1', 'signature': self.signature}
