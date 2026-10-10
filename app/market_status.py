"""Read-only market display derived from a currently bound, dated calendar cache.

No broker client, network request, refresh or scheduler mutation belongs here.
This describes the declared calendar, not a live venue-open observation.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from framework.market import ExecutionContext
from framework.scheduling import normalize_sessions


def _calendar_state(context, cache, checked_field, at):
    if not isinstance(cache, dict) or cache.get(checked_field) != at.astimezone(ZoneInfo(context.timezone)).date().isoformat():
        return None
    sessions = normalize_sessions(cache.get('sessions'))
    if not sessions or any(not item.intervals for item in sessions):
        return None  # Date-only compatibility inputs do not prove opening hours.
    today = at.astimezone(ZoneInfo(context.timezone)).date()
    active = next((item for item in sessions if any(opening <= at < close for opening, close in item.intervals)), None)
    session = active or next((item for item in sessions if item.day == today), None)
    if session is None:
        return None  # An absent record alone does not prove a holiday or full calendar coverage.
    if active is not None:
        status, label = 'open', '交易时段（日历）'
    elif session.intervals[0][0] <= at < session.intervals[-1][1]:
        status, label = 'break', '休市间隔（日历）'
    else:
        status, label = 'closed', '休市（日历）'
    return {'status': status, 'label': label, 'observed_at': at.isoformat(),
            'source': 'calendar_cache', 'calendar_checked_day': cache[checked_field],
            'session_day': session.day.isoformat(), 'market': context.market, 'timezone': context.timezone}


def selected_market_session(broker, *, at, current_context, automation, instances,
                            connection_context=None):
    """Return None unless freshness, market and selected-account provenance all agree.

Automation resets its calendar when binding a plan, so its context supplies
cache provenance. Older instance caches lack a connection revision; they stay
unknown until a producer explicitly supplies a matching connection_context.
"""
    try:
        if not isinstance(broker, dict) or not broker.get('enabled') or not broker.get('id'):
            return None
        if not isinstance(at, datetime) or at.tzinfo is None:
            return None
        context = ExecutionContext.from_record(broker.get('execution_context'))
        binding = automation.get('binding') if isinstance(automation, dict) else None
        if (isinstance(binding, dict) and binding.get('broker_id') == broker['id']
                and current_context is not None and binding.get('context') == current_context
                and automation.get('config', {}).get('timezone') == context.timezone):
            result = _calendar_state(context, automation.get('calendar'), 'checked_day', at)
            if result is not None:
                return dict(result, broker_id=broker['id'])
        cache = instances.get('calendar', {}).get(broker['id']) if isinstance(instances, dict) else None
        if isinstance(cache, dict) and isinstance(cache.get('connection_context'), str) and callable(connection_context):
            current = connection_context()
            if current and cache['connection_context'] == current:
                result = _calendar_state(context, cache, 'day', at)
                if result is not None:
                    return dict(result, broker_id=broker['id'])
    except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OSError, ArithmeticError):
        return None
    return None
