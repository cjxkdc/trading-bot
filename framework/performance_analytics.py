"""Range analytics from observed NAV and explicitly reviewed external cash flows."""
from calendar import monthrange
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
PERIODS = ('1W', '1M', '3M', 'YTD', 'ALL')


def at(value):
    return datetime.fromisoformat(value)


def cutoff(latest, period):
    local = at(latest).astimezone(NY)
    if period == 'ALL':
        return None
    if period == '1W':
        return local - timedelta(days=7)
    if period == 'YTD':
        return local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    months = 1 if period == '1M' else 3
    year, month = divmod(local.year * 12 + local.month - 1 - months, 12)
    month += 1
    return local.replace(year=year, month=month, day=min(local.day, monthrange(year, month)[1]))


def analyze_window(items, flows, reviewed_through):
    if not items:
        return dict(start=None, end=None, points=[], metrics={}, verified=None, observations=0)
    base = items[0]
    external = [f for f in flows if f['kind'] in {'deposit', 'withdrawal'}]
    reviewed = at(reviewed_through) if reviewed_through else None
    total = 0.0
    nav = peak = 1.0
    drawdown = 0.0
    valid = True
    points = []
    verified = None
    for i, item in enumerate(items):
        if i:
            prev = items[i - 1]
            start, end = at(prev['as_of']), at(item['as_of'])
            span = (end - start).total_seconds()
            interval = [f for f in external if start < at(f['as_of']) <= end]
            net = sum(f['amount'] * (1 if f['kind'] == 'deposit' else -1) for f in interval)
            weighted = sum(f['amount'] * (1 if f['kind'] == 'deposit' else -1)
                           * (end - at(f['as_of'])).total_seconds() / span for f in interval)
            total += net
            denominator = prev['equity'] + weighted
            if valid and denominator > 0 and math.isfinite(denominator):
                nav *= 1 + (item['equity'] - prev['equity'] - net) / denominator
                valid = nav > 0 and math.isfinite(nav)
            else:
                valid = False
            if valid:
                peak = max(peak, nav)
                drawdown = max(drawdown, 1 - nav / peak)
        known = bool(reviewed and at(item['as_of']) <= reviewed)
        profit = item['equity'] - base['equity'] - total
        profit = profit if known and math.isfinite(profit) else None
        rate = nav - 1 if known and valid else None
        point = dict(as_of=item['as_of'], equity=item['equity'], profit=profit, return_rate=rate)
        points.append(point)
        if known and i:
            verified = dict(through=item['as_of'], profit=profit, return_rate=rate,
                            max_drawdown=drawdown if valid else None, net_flow=total)
    complete = bool(verified and verified['through'] == items[-1]['as_of'])
    metrics = dict(profit=points[-1]['profit'] if len(items) > 1 and complete else None,
                   return_rate=points[-1]['return_rate'] if len(items) > 1 and complete else None,
                   max_drawdown=drawdown if len(items) > 1 and complete and valid else None,
                   asset_change=items[-1]['equity'] - base['equity'],
                   net_flow=total if complete else None, flow_review_required=not complete)
    return dict(start=base['as_of'], end=items[-1]['as_of'], points=points, metrics=metrics,
                verified=verified, observations=len(items))


def period_reports(items, flows, reviewed_through):
    result = {}
    for period in PERIODS:
        lower = cutoff(items[-1]['as_of'], period) if items else None
        # Never invent a boundary price or borrow an observation outside the range.
        window = [item for item in items if lower is None or at(item['as_of']) >= lower]
        result[period] = analyze_window(window, flows, reviewed_through)
        result[period]['requested_start'] = lower.isoformat() if lower else None
    return result


def position_summary(positions):
    held = [p for p in positions if p['quantity'] > 0]
    values = [p.get('position_pnl') for p in held]
    covered = sum(value is not None for value in values)
    return dict(position_count=len(held), pnl_covered_count=covered,
                position_pnl=sum(values) if covered == len(held) else None)
