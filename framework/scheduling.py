"""Daily, weekly and monthly schedules using broker-supplied exchange sessions."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_SCHEDULE = dict(cadence='monthly', day=1, weekday=0, prepare_time='06:00', trade_time='09:35',
                        window_minutes=60, timezone='America/New_York', poll_seconds=30, initial_entry='immediate')


def schedule_config(value):
    if not isinstance(value, dict) or set(value) - DEFAULT_SCHEDULE.keys():
        raise ValueError('Unknown schedule fields')
    cfg = dict(DEFAULT_SCHEDULE, **value)
    if cfg['initial_entry'] not in {'immediate', 'scheduled'}:
        raise ValueError('Invalid initial entry rule')
    if cfg['cadence'] not in {'daily', 'weekly', 'monthly'} or cfg['timezone'] != 'America/New_York':
        raise ValueError('Unsupported US equity schedule')
    for key, low, high in [('day', 1, 28), ('weekday', 0, 4), ('window_minutes', 5, 120), ('poll_seconds', 15, 300)]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError('Invalid schedule number')
    for key in ('prepare_time', 'trade_time'):
        text = cfg[key]
        if not isinstance(text, str) or len(text) != 5 or text[2] != ':':
            raise ValueError('Time must be HH:MM')
        t = time.fromisoformat(text)
        if t.isoformat(timespec='minutes') != text:
            raise ValueError('Time must be HH:MM')
    if not '00:00' <= cfg['prepare_time'] < cfg['trade_time'] or not '09:35' <= cfg['trade_time'] <= '14:00':
        raise ValueError('Preparation must precede the regular-hours execution window')
    return cfg


def instant(day, hhmm, cfg):
    return datetime.combine(day, time.fromisoformat(hhmm), ZoneInfo(cfg['timezone']))


def month_window(day, sessions, cfg):
    anchor = day.replace(day=cfg['day'])
    eligible = sorted(s for s in sessions if s.year == day.year and s.month == day.month and s >= anchor)
    if not eligible:
        raise ValueError('Exchange calendar does not cover the monthly anchor')
    start = instant(eligible[0], cfg['trade_time'], cfg)
    return start, start + timedelta(minutes=cfg['window_minutes'])


def period_key(day, cfg):
    if cfg['cadence'] == 'monthly':
        return day.strftime('%Y-%m')
    if cfg['cadence'] == 'weekly':
        return 'week-' + str(day - timedelta(days=day.weekday()))
    return str(day)


def schedule_window(day, sessions, cfg):
    if cfg['cadence'] == 'monthly':
        return month_window(day, sessions, cfg)
    if cfg['cadence'] == 'weekly':
        monday = day - timedelta(days=day.weekday())
        anchor = monday + timedelta(days=cfg['weekday'])
        eligible = sorted(s for s in sessions if anchor <= s < monday + timedelta(days=14))
    else:
        eligible = [day] if day in sessions else []
    if not eligible:
        raise ValueError('No exchange session in this schedule period')
    start = instant(eligible[0], cfg['trade_time'], cfg)
    return start, start + timedelta(minutes=cfg['window_minutes'])


def period_window(day, sessions, cfg):
    # A Friday holiday may move its weekly window into the following Monday.
    if cfg['cadence'] == 'weekly':
        previous = day - timedelta(days=day.weekday() + 7)
        try:
            start, end = schedule_window(previous, sessions, cfg)
            if start.date() == day:
                return start, end, period_key(previous, cfg)
        except ValueError:
            pass
    start, end = schedule_window(day, sessions, cfg)
    return start, end, period_key(day, cfg)


def initial_window(now, sessions, cfg):
    """The first portfolio starts at the next usable regular session, independent of cadence."""
    local = now.astimezone(ZoneInfo(cfg['timezone']))
    if cfg.get('initial_entry', 'immediate') == 'scheduled':
        start = next_window(now, sessions, cfg)
        if start is None:
            raise ValueError('Exchange calendar does not cover initial schedule')
        return max(start, local), start + timedelta(minutes=cfg['window_minutes'])
    for day in sorted(s for s in sessions if s >= local.date()):
        opening, close = instant(day, '09:35', cfg), instant(day, '16:00', cfg)
        start = max(opening, local) if day == local.date() else opening
        # Leave enough time for a complete quote/account preparation near the close.
        if start >= close - timedelta(minutes=5):
            continue
        return start, min(start + timedelta(minutes=cfg['window_minutes']), close)
    raise ValueError('Exchange calendar does not cover the initial portfolio')


def selection_period(day, sessions, cfg, *, first=False):
    """Shared completed-session trigger for QC and the broker calendar scheduler."""
    start, _, key = period_window(day, sessions, cfg)
    if day not in sessions:
        return None
    if first and cfg.get('initial_entry', 'immediate') == 'immediate':
        return key
    return key if start.date() == day else None


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
