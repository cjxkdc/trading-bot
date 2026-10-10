"""Queued historical collection of the snapshot series: one dated snapshot per job, resumable."""
from copy import deepcopy
from datetime import date, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4

from app.atomic import replace_file

CADENCES = ('monthly', 'weekly')
MAX_DATES = 120
FINAL = {'done', 'skipped', 'cancelled'}


def next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def schedule_dates(start, end, cadence, today):
    """Anchor dates inside [start, end] and before today: month starts or Mondays.

    A snapshot anchored on a date is built from the sessions that ended the day
    before, so a month-start anchor records the previous month's close.
    """
    if cadence not in CADENCES:
        raise ValueError('cadence')
    days = []
    if cadence == 'monthly':
        day = date(start.year, start.month, 1)
        if day < start:
            day = next_month(day)
        while day <= end:
            if day < today:
                days.append(day)
            day = next_month(day)
    else:
        day = start + timedelta(days=(7 - start.weekday()) % 7)
        while day <= end:
            if day < today:
                days.append(day)
            day += timedelta(days=7)
    return days


def covered(day, existing, window=7):
    """A snapshot dated within the week before the anchor already stands for it."""
    return next((d for d in sorted(existing, reverse=True) if day - timedelta(days=window) <= d < day), None)


def estimate_requests(count, *, history_bars, details, income_required, listing_pages, cadence):
    """Rough request count: the first date downloads the whole history, later ones only the new sessions."""
    fixed = 5 + listing_pages  # SPY history, two AAPL probes, grouped probe, splits, universe pages
    fresh = 23 if cadence == 'monthly' else 5
    total = 0
    for index in range(count):
        sessions = history_bars if index == 0 else min(history_bars, fresh)
        total += fixed + sessions + details * (2 if income_required else 1)
    return total


class Backfill:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'runs/portal/backfill.json'
        self.data = None
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and raw.get('schema_version') == 1 and isinstance(raw.get('dates'), list):
                self.data = raw
        except (OSError, ValueError):
            pass
        if self.data and (self.data.get('status') == 'running'
                          or any(item.get('status') == 'running' for item in self.data['dates'])):
            # A date that was collecting when the service stopped never reported; it is retried after a look.
            for item in self.data['dates']:
                if item.get('status') == 'running':
                    item.update(status='pending', job_id=None, message='上次服务退出前未完成，继续时重新采集。')
            self.data.update(status='paused', message='服务重启后回填已暂停，请确认后继续。')
            self.persist()

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as handle:
                handle.write(json.dumps(self.data, ensure_ascii=False, indent=2, allow_nan=False))
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def active(self):
        return bool(self.data) and self.data['status'] in {'running', 'paused'}

    def public(self):
        if not self.data:
            return None
        counts = {}
        for item in self.data['dates']:
            counts[item['status']] = counts.get(item['status'], 0) + 1
        current = next((item['as_of'] for item in self.data['dates'] if item['status'] == 'running'), None)
        return dict(deepcopy(self.data), counts=counts, current=current, total=len(self.data['dates']),
                    finished_count=sum(counts.get(key, 0) for key in FINAL | {'failed'}))

    def create(self, *, source, strategy_type, request_id, cadence, start, end, dates, estimate, now):
        if self.active:
            raise ValueError('backfill already active')
        self.data = {'schema_version': 1, 'source': source, 'strategy_type': strategy_type, 'request_id': request_id,
                     'cadence': cadence, 'start': str(start), 'end': str(end), 'status': 'running',
                     'message': '等待工作台空闲后开始采集。', 'created_at': now, 'updated_at': now, 'estimate': estimate,
                     'dates': [dict(item) for item in dates]}
        self.persist()
        return self.public()

    def next_pending(self):
        if not self.data:
            return None
        return next((item for item in self.data['dates'] if item['status'] == 'pending'), None)

    def begin(self, item, job_id, now):
        item.update(status='running', job_id=job_id, message='正在采集…')
        self.data.update(message=f"正在采集 {item['as_of']}…", updated_at=now)
        self.persist()

    def record(self, as_of, result, now):
        """One job reported; a failure pauses the queue so a permission problem does not burn requests."""
        item = next((i for i in self.data['dates'] if i['as_of'] == as_of), None) if self.data else None
        if item is None:
            return
        details = result.get('details') if isinstance(result.get('details'), dict) else {}
        message = str(result.get('message', ''))[:300]
        if result.get('status') == 'complete':
            item.update(status='done', job_id=None, message=message, price_as_of=details.get('price_as_of'),
                        usable_count=details.get('usable_count'), network_requests=details.get('network_requests'))
            remaining = sum(i['status'] == 'pending' for i in self.data['dates'])
            if remaining:
                self.data.update(message=f'{as_of} 已完成，还有 {remaining} 个日期。')
            elif self.data['status'] == 'running':
                self.data.update(status='finished', message='回填完成。')
        else:
            item.update(status='failed', job_id=None, message=message)
            if self.data['status'] in {'running', 'paused'}:
                self.data.update(status='paused', message=f'{as_of} 采集未完成：{message} 回填已暂停，处理后可继续。')
        self.data['updated_at'] = now
        self.persist()

    def pause(self, message, now):
        if self.data and self.data['status'] == 'running':
            self.data.update(status='paused', message=message, updated_at=now)
            self.persist()

    def resume(self, now):
        if not self.data or self.data['status'] != 'paused':
            raise ValueError('not paused')
        for item in self.data['dates']:
            if item['status'] == 'failed':
                item.update(status='pending', message='等待重新采集。')
        self.data.update(status='running', message='已继续，等待工作台空闲。', updated_at=now)
        self.persist()

    def cancel(self, now):
        if not self.active:
            raise ValueError('not active')
        for item in self.data['dates']:
            if item['status'] in {'pending', 'failed'}:
                item.update(status='cancelled', message='已取消。')
        self.data.update(status='cancelled', message='回填已取消；已完成的快照保留。', updated_at=now)
        self.persist()

    def finish(self, now):
        if self.data and self.data['status'] == 'running':
            self.data.update(status='finished', message='回填完成。', updated_at=now)
            self.persist()
