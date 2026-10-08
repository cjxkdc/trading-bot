"""Calendar boundaries for observed account equity; never infer unobserved prices."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Default only for the legacy USD observations stored before market contexts.
LEGACY_TIMEZONE = 'America/New_York'
PERIODS = {'1m': 60, '5m': 300, '1h': 3600, '1d': None, '1w': None}
# All timestamps are normalized to UTC when stored. Daily/week keys use the
# separately stored New York calendar date, including 23/25-hour DST days.
KEY_SQL = {
    '1m': "substr(as_of,1,16) || ':00+00:00'",
    '5m': "printf('%s%02d:00+00:00',substr(as_of,1,14),(CAST(substr(as_of,15,2) AS INTEGER)/5)*5)",
    '1h': "substr(as_of,1,13) || ':00:00+00:00'",
    '1d': 'day',
    '1w': "date(day,'weekday 0','-6 days')",
}


def validate_period(interval, limit):
    if type(interval) is not str or interval not in PERIODS or type(limit) is not int or not 1 <= limit <= 240:
        raise ValueError('Unsupported account candle period or range')


def bounds(at, interval, market_timezone=LEGACY_TIMEZONE):
    """Return a half-open UTC window. Repeated DST hours stay distinct."""
    if at.tzinfo is None or interval not in PERIODS:
        raise ValueError('Candle timestamps require a timezone and known period')
    seconds = PERIODS[interval]
    if seconds:
        start = datetime.fromtimestamp((int(at.timestamp()) // seconds) * seconds, timezone.utc)
        return start, start + timedelta(seconds=seconds)
    local = at.astimezone(ZoneInfo(market_timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if interval == '1w':
        start -= timedelta(days=start.weekday())
    end = start + timedelta(days=7 if interval == '1w' else 1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def window_start(latest, interval, limit, market_timezone=LEGACY_TIMEZONE):
    validate_period(interval, limit)
    start, _ = bounds(latest, interval, market_timezone)
    seconds = PERIODS[interval]
    if seconds:
        return start - timedelta(seconds=seconds * (limit - 1))
    return (start.astimezone(ZoneInfo(market_timezone)) - timedelta(days=(7 if interval == '1w' else 1) * (limit - 1))).astimezone(timezone.utc)
