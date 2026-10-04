"""Bounded, read-only server projection; never a remote shell or credential view."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import shutil
import threading
import time

from app.deployment_metadata import deployment_links, load_deployment


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return int(value) if integer else value


def label(value, maximum=100):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        return None
    return value


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.isoformat() if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


class HostMetrics:
    """Linux host metrics; an unavailable counter remains unknown, including CPU first sample."""
    def __init__(self, root, *, proc=Path('/proc')):
        self.root, self.proc = Path(root), Path(proc)
        self.previous_cpu = self._cpu()

    def _cpu(self):
        try:
            with (self.proc / 'stat').open(encoding='ascii') as stream:
                fields = stream.readline(1024).split()
            if fields[0] != 'cpu' or len(fields) < 5:
                return None
            counters = [int(x) for x in fields[1:9]]
            if any(x < 0 for x in counters):
                return None
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
            return sum(counters), idle
        except (OSError, ValueError, IndexError):
            return None

    def sample(self):
        result = dict(os=None, cpu_count=None, cpu_percent=None, memory_total_bytes=None,
                      memory_used_bytes=None, disk_total_bytes=None, disk_used_bytes=None,
                      uptime_seconds=None, sampled_at=utcnow())
        try:
            result['os'] = label(platform.system() + ' ' + platform.release(), 160)
            result['cpu_count'] = number(os.cpu_count(), integer=True)
        except OSError:
            pass
        current = self._cpu()
        previous, self.previous_cpu = self.previous_cpu, current
        if current is not None and previous is not None:
            total, idle = current[0]-previous[0], current[1]-previous[1]
            if total > 0 and 0 <= idle <= total:
                result['cpu_percent'] = round(100 * (total-idle) / total, 1)
        try:
            memory = {}
            with (self.proc / 'meminfo').open(encoding='ascii') as stream:
                for line in stream.read(16384).splitlines():
                    fields = line.split()
                    if len(fields) == 3 and fields[0] in {'MemTotal:', 'MemAvailable:'} and fields[2] == 'kB':
                        memory[fields[0]] = int(fields[1])*1024
            total, available = memory['MemTotal:'], memory['MemAvailable:']
            if total > 0 and 0 <= available <= total:
                result.update(memory_total_bytes=total, memory_used_bytes=total-available)
        except (OSError, ValueError, KeyError):
            pass
        try:
            usage = shutil.disk_usage(self.root)
            if 0 <= usage.used <= usage.total and usage.total > 0:
                result.update(disk_total_bytes=int(usage.total), disk_used_bytes=int(usage.used))
        except (OSError, ValueError):
            pass
        try:
            result['uptime_seconds'] = number(float((self.proc/'uptime').read_text(encoding='ascii')[:100].split()[0]), integer=True)
        except (OSError, ValueError, IndexError):
            pass
        return result


class ServerStatus:
    def __init__(self, root, runtime, *, metrics=None, clock=time.monotonic):
        self.root, self.runtime, self.clock = Path(root), runtime, clock
        self.metrics = metrics or HostMetrics(root)
        self.started = clock()
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = None
        self.version = self._version()

    def _version(self):
        path = self.root/'release-manifest.json'
        try:
            if path.stat().st_size > 2*1024*1024:
                return None
            content = path.read_bytes()
            if not isinstance(json.loads(content), dict):
                return None
            return 'build-' + sha256(content).hexdigest()[:12]
        except (OSError, ValueError):
            return None

    def invalidate(self):
        with self.lock:
            self.cached_at = None

    def _runtime(self):
        try:
            raw = self.runtime.status()  # Exactly one health check, bounded to 1 second by runtime.
            if not isinstance(raw, dict):
                raise ValueError()
        except Exception:
            raw = {}
        phase = raw.get('status')
        if phase not in {'starting', 'running', 'unresponsive', 'stopping', 'stopped', 'failed'}:
            phase = 'unknown'
        return dict(status=phase, alive=raw.get('alive') is True,
                    started_at=timestamp(raw.get('started_at')), checked_at=timestamp(raw.get('checked_at')),
                    uptime_seconds=number(raw.get('uptime_seconds'), integer=True),
                    pid=number(raw.get('pid'), integer=True))

    def _backend(self, runtime):
        result = dict(reachable=False, active_job=False, automation_enabled=None, automation_status=None,
                      broker_name=None, broker_status=None, broker_checked_at=None)
        if runtime['status'] not in {'running', 'unresponsive'} or not runtime['alive']:
            return result
        try:
            # Fixed internal path; do not forward browser headers/cookies. Never call
            # runtime.forward here, since its 30-second limit is for deliberate actions.
            reply = self.runtime._request('GET', '/api/state', timeout=2)
            if reply.status != 200 or len(reply.body) > 2*1024*1024:
                return result
            raw = json.loads(reply.body)
            if not isinstance(raw, dict):
                return result
            if 'active' not in raw or (raw['active'] is not None and not isinstance(raw['active'], dict)):
                return result  # Unknown task state must never be advertised as safe to stop.
            automation = raw.get('automation') if isinstance(raw.get('automation'), dict) else {}
            broker = raw.get('broker') if isinstance(raw.get('broker'), dict) else {}
            account = raw.get('account') if isinstance(raw.get('account'), dict) else {}
            auto_status = automation.get('status')
            account_status = account.get('status')
            result.update(reachable=True, active_job=raw['active'] is not None,
                          automation_enabled=automation.get('enabled') if type(automation.get('enabled')) is bool else None,
                          automation_status=auto_status if auto_status in {'disabled','waiting','running','paused'} else None,
                          broker_name=label(broker.get('name')),
                          broker_status=account_status if account_status in {'connected','ready','blocked','paused','disconnected'} else None,
                          broker_checked_at=timestamp(account.get('checked_at')))
        except Exception:
            # Vendor exception messages may contain credentials. Unknown stays unknown.
            pass
        return result

    def _operations(self, runtime, backend):
        starting = runtime['status'] in {'stopped','failed'} and not runtime['alive']
        stopping = runtime['status']=='running' and backend['reachable'] and not backend['active_job']
        if getattr(self.runtime, 'closing', False):
            starting = stopping = False
        return [dict(id='start', enabled=starting, reason=None if starting else '后台已运行或正在切换状态。'),
                dict(id='stop', enabled=stopping, reason=None if stopping else
                     '任务仍在执行，请等待完成。' if backend['active_job'] else '后台未运行或状态尚未核实。')]

    def status(self):
        # Short cache coalesces concurrent UI polls and never refreshes its timestamps
        # without resampling. All work is local/proc + at most 1s health and 2s state.
        with self.lock:
            if self.cached_at is not None and self.clock()-self.cached_at < 1:
                return deepcopy(self.cached)
            metadata = load_deployment(self.root)
            host = self.metrics.sample()
            runtime = self._runtime()
            backend = self._backend(runtime)
            result = dict(checked_at=utcnow(),
                          deployment={k:metadata.get(k) for k in ('provider','region','instance_name')},
                          host=host, runtime=runtime, backend=backend,
                          portal=dict(status='running', uptime_seconds=max(0,int(self.clock()-self.started)), version=self.version),
                          links=deployment_links(metadata), operations=self._operations(runtime,backend))
            self.cached, self.cached_at = deepcopy(result), self.clock()
            return result
