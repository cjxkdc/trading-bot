"""Schedules over explicit module-supplied sessions in any IANA timezone.

Date-only calendars are a legacy US-equity compatibility input. New modules
provide each session's actual intervals, including breaks and early closes.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LEGACY_US_SCHEDULE = dict(cadence='monthly', day=1, weekday=0, prepare_time='06:00', trade_time='09:35',
                        window_minutes=60, timezone='America/New_York', poll_seconds=30, initial_entry='immediate')
DEFAULT_SCHEDULE = LEGACY_US_SCHEDULE  # Existing saved plans retain their original interpretation.


@dataclass(frozen=True)
class TradingSession:
    day: date
    intervals: tuple[tuple[datetime, datetime], ...] = ()

    def record(self):
        if not self.intervals:
            return self.day.isoformat()
        return {'date': self.day.isoformat(), 'intervals': [
            {'open': opening.isoformat(), 'close': close.isoformat()} for opening, close in self.intervals]}


def normalize_sessions(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError('Calendar sessions must be a list')
    result = []
    for value in values:
        if isinstance(value, TradingSession):
            item = value
        elif isinstance(value, date) and not isinstance(value, datetime):
            item = TradingSession(value)
        elif isinstance(value, str):
            item = TradingSession(date.fromisoformat(value))
        elif isinstance(value, dict) and set(value) == {'date', 'intervals'}:
            day = date.fromisoformat(value['date'])
            intervals = []
            if not isinstance(value['intervals'], (list, tuple)) or not value['intervals']:
                raise ValueError('Explicit sessions require trading intervals')
            for interval in value['intervals']:
                if not isinstance(interval, dict) or set(interval) != {'open', 'close'}:
                    raise ValueError('Invalid session interval')
                opening, close = (datetime.fromisoformat(interval[k]) for k in ('open', 'close'))
                if opening.tzinfo is None or close.tzinfo is None or close <= opening:
                    raise ValueError('Session intervals require increasing aware timestamps')
                if intervals and opening < intervals[-1][1]:
                    raise ValueError('Trading intervals overlap or are out of order')
                intervals.append((opening, close))
            item = TradingSession(day, tuple(intervals))
        else:
            raise ValueError('Invalid calendar session')
        result.append(item)
    if len({x.day for x in result}) != len(result):
        raise ValueError('Duplicate calendar session dates')
    # Sessions also may not overlap across business dates (overnight is valid).
    actual = sorted((op, cl) for item in result for op, cl in item.intervals)
    if any(actual[i][0] < actual[i-1][1] for i in range(1, len(actual))):
        raise ValueError('Calendar sessions overlap')
    return tuple(sorted(result, key=lambda x: x.day))


def session_dates(values):
    return tuple(x.day for x in normalize_sessions(values))


def schedule_defaults(context=None):
    if context is None:
        return dict(LEGACY_US_SCHEDULE)
    from framework.market import ExecutionContext
    market = ExecutionContext.from_record(context)
    defaults = dict(LEGACY_US_SCHEDULE, timezone=market.timezone)
    if market.market != "US" or market.asset_class != "equity":
        defaults.update(prepare_time="00:00", trade_time="00:00")
    return defaults


def schedule_config(value, *, defaults=None):
    if not isinstance(value, dict) or set(value) - DEFAULT_SCHEDULE.keys():
        raise ValueError('Unknown schedule fields')
    cfg = dict(DEFAULT_SCHEDULE if defaults is None else defaults, **value)
    if cfg['initial_entry'] not in {'immediate', 'scheduled'}:
        raise ValueError('Invalid initial entry rule')
    if cfg['cadence'] not in {'daily', 'weekly', 'monthly'}:
        raise ValueError('Unsupported schedule cadence')
    try:
        if not isinstance(cfg['timezone'], str):
            raise ValueError()
        ZoneInfo(cfg['timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('Schedule timezone must be a valid IANA timezone') from None
    for key, low, high in [('day', 1, 28), ('weekday', 0, 6), ('window_minutes', 1, 1440), ('poll_seconds', 15, 300)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid schedule number')
    for key in ('prepare_time', 'trade_time'):
        text = cfg[key]
        if not isinstance(text, str) or len(text) != 5 or text[2] != ':':
            raise ValueError('Time must be HH:MM')
        t = time.fromisoformat(text)
        if t.isoformat(timespec='minutes') != text:
            raise ValueError('Time must be HH:MM')
    if cfg['prepare_time'] > cfg['trade_time']:
        raise ValueError('Preparation time must not follow execution time on the scheduled day')
    return cfg


def instant(day, hhmm, cfg):
    return datetime.combine(day, time.fromisoformat(hhmm), ZoneInfo(cfg['timezone']))


def session_intervals(session, cfg):
    if session.intervals:
        return session.intervals
    if cfg['timezone'] != LEGACY_US_SCHEDULE['timezone']:
        raise ValueError('This market module must provide explicit session intervals')
    return ((instant(session.day, '09:35', cfg), instant(session.day, '16:00', cfg)),)


def session_close(day, sessions, cfg):
    session = next((s for s in normalize_sessions(sessions) if s.day == day), None)
    if session is None:
        raise ValueError('Calendar does not contain this trading session')
    return session_intervals(session, cfg)[-1][1]


def _window_for_day(day, sessions, cfg):
    session = next((s for s in normalize_sessions(sessions) if s.day == day), None)
    if session is None:
        raise ValueError('No exchange session in this schedule period')
    scheduled = instant(day, cfg['trade_time'], cfg)
    for opening, close in session_intervals(session, cfg):
        start = max(scheduled, opening)
        if start < close:
            return start, min(start + timedelta(minutes=cfg['window_minutes']), close)
    raise ValueError('Configured execution time is outside this market session')


def month_window(day, sessions, cfg):
    anchor = day.replace(day=cfg['day'])
    eligible = sorted(s for s in session_dates(sessions) if s.year == day.year and s.month == day.month and s >= anchor)
    if not eligible:
        raise ValueError('Exchange calendar does not cover the monthly anchor')
    return _window_for_day(eligible[0], sessions, cfg)


def period_key(day, cfg):
    if cfg['cadence'] == 'monthly':
        return day.strftime('%Y-%m')
    if cfg['cadence'] == 'weekly':
        return 'week-' + str(day - timedelta(days=day.weekday()))
    return str(day)


def schedule_window(day, sessions, cfg):
    if cfg['cadence'] == 'monthly':
        return month_window(day, sessions, cfg)
    days = session_dates(sessions)
    if cfg['cadence'] == 'weekly':
        monday = day - timedelta(days=day.weekday())
        anchor = monday + timedelta(days=cfg['weekday'])
        eligible = sorted(s for s in days if anchor <= s < monday + timedelta(days=14))
    else:
        eligible = [day] if day in days else []
    if not eligible:
        raise ValueError('No exchange session in this schedule period')
    return _window_for_day(eligible[0], sessions, cfg)


def period_window(day, sessions, cfg):
    if cfg['cadence'] == 'weekly':
        previous = day - timedelta(days=day.weekday() + 7)
        try:
            start, end = schedule_window(previous, sessions, cfg)
            if start.astimezone(ZoneInfo(cfg['timezone'])).date() == day:
                return start, end, period_key(previous, cfg)
        except ValueError:
            pass
    start, end = schedule_window(day, sessions, cfg)
    return start, end, period_key(day, cfg)


def initial_window(now, sessions, cfg):
    """Start at a usable broker-declared interval; never run through a market break."""
    local = now.astimezone(ZoneInfo(cfg['timezone']))
    if cfg.get('initial_entry', 'immediate') == 'scheduled':
        start = next_window(now, sessions, cfg)
        if start is None:
            raise ValueError('Exchange calendar does not cover initial schedule')
        for session in normalize_sessions(sessions):
            for opening, close in session_intervals(session, cfg):
                if opening <= start < close:
                    return max(start, local), min(start + timedelta(minutes=cfg['window_minutes']), close)
        raise ValueError('Scheduled execution is outside supplied sessions')
    for session in normalize_sessions(sessions):
        for opening, close in session_intervals(session, cfg):
            start = max(opening, local)
            if start >= close - timedelta(minutes=5):
                continue
            return start, min(start + timedelta(minutes=cfg['window_minutes']), close)
    raise ValueError('Exchange calendar does not cover the initial portfolio')


def selection_period(day, sessions, cfg, *, first=False):
    start, _, key = period_window(day, sessions, cfg)
    if day not in session_dates(sessions):
        return None
    if first and cfg.get('initial_entry', 'immediate') == 'immediate':
        return key
    return key if start.astimezone(ZoneInfo(cfg['timezone'])).date() == day else None


def next_window(now, sessions, cfg):
    local = now.astimezone(ZoneInfo(cfg['timezone']))
    cursor = local.date() - timedelta(days=7) if cfg['cadence'] == 'weekly' else local.date()
    for _ in range(100):
        try:
            start, end = schedule_window(cursor, sessions, cfg)
            if end > local:
                return start
        except ValueError:
            pass
        if cfg['cadence'] == 'monthly':
            cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        elif cfg['cadence'] == 'weekly':
            cursor = cursor - timedelta(days=cursor.weekday()) + timedelta(days=7)
        else:
            cursor += timedelta(days=1)
    return None
