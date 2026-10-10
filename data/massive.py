"""Read-only Massive REST transport. No broker SDK or account access.

Credentials are headers only. Requests, pagination, rate and disk caches are
bounded; provider errors are never copied verbatim to the UI or log.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from data.common import DataIssue, utcnow, write_json

BASE = 'https://api.massive.com'
INCOME = '/stocks/financials/v1/income-statements'
TICKERS = '/v3/reference/tickers'
SPLITS = '/stocks/v1/splits'
TICKER = re.compile(r'[A-Z][A-Z0-9.\-]{0,19}')
PATHS = re.compile(r'(?:/v3/reference/tickers(?:/[A-Z][A-Z0-9.\-]{0,19})?'
                   r'|/stocks/financials/v1/income-statements'
                   r'|/stocks/v1/splits'
                   r'|/v2/aggs/grouped/locale/us/market/stocks/\d{4}-\d{2}-\d{2}'
                   r'|/v2/aggs/ticker/[A-Z][A-Z0-9.\-]{0,19}/range/1/day/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2})')


class MassiveError(DataIssue):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    requests_per_minute: int = 5


def validate_settings(key, requests_per_minute=5):
    if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.=\-]{16,256}', key)
            or key.upper().startswith(('YOUR_', 'REPLACE_', 'ENTER_'))):
        raise MassiveError('missing_key', '请在「配置 Massive」中保存你自己的 API Key。')
    if type(requests_per_minute) is not int or not 1 <= requests_per_minute <= 600:
        raise MassiveError('invalid_rate', '请求速度必须是 1–600 次/分钟，并符合你的套餐。')
    return Settings(key, requests_per_minute)


def settings(config_dir):
    path = Path(config_dir) / 'connection.json'
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise MassiveError('invalid_config', 'Massive 本地配置无法读取，请重新配置。') from None
        if not isinstance(raw, dict) or set(raw) - {'api_key', 'requests_per_minute'}:
            raise MassiveError('invalid_config', 'Massive 配置字段无效。')
        return validate_settings(raw.get('api_key'), raw.get('requests_per_minute', 5))
    return validate_settings(os.environ.get('MASSIVE_API_KEY'), 5)


def save_settings(config_dir, key, requests_per_minute=5):
    cfg = validate_settings(key.strip(), requests_per_minute)
    path = Path(config_dir) / 'connection.json'
    # Atomic private file; caller owns the explicitly selected secrets directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4
    temporary = path.with_name('.connection-' + uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as out:
            json.dump({'api_key': cfg.api_key, 'requests_per_minute': cfg.requests_per_minute}, out)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configuration_status(config_dir):
    try:
        cfg = settings(config_dir)
        return {'configured': True, 'requests_per_minute': cfg.requests_per_minute,
                'message': 'Massive 密钥已配置，尚未联网验证。', 'network_requested': False}
    except MassiveError as exc:
        return {'configured': False, 'message': str(exc), 'network_requested': False}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def canonical_url(value, *, expected_path=None):
    value = BASE + value if value.startswith('/') and not value.startswith('//') else value
    try:
        part = urlsplit(value)
        valid = (part.scheme == 'https' and part.netloc == 'api.massive.com'
                 and not part.fragment and PATHS.fullmatch(part.path)
                 and (expected_path is None or part.path == expected_path))
        if not valid:
            raise ValueError()
        pairs = parse_qsl(part.query, keep_blank_values=True, strict_parsing=True)
        # Some next_url responses carry the key. Discard it; never persist it.
        pairs = [(k, v) for k, v in pairs if k.lower() not in {'apikey', 'api_key', 'access_token'}]
        if len(pairs) != len({k for k, _ in pairs}):
            raise ValueError()
        return urlunsplit(('https', 'api.massive.com', part.path, urlencode(sorted(pairs)), ''))
    except (ValueError, TypeError, AttributeError):
        raise MassiveError('unsafe_url', 'Massive 返回了不支持的分页地址，下载已停止。') from None


def _scrub(value, key):
    if isinstance(value, dict):
        return {k: _scrub(v, key) for k, v in value.items()
                if k.lower() not in {'apikey', 'api_key', 'authorization', 'access_token'}}
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    return value.replace(key, '[REDACTED]') if isinstance(value, str) else value


def _payload_digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class MassiveClient:
    def __init__(self, config, cache_dir, *, max_requests=10000, opener=None,
                 clock=utcnow, monotonic=time.monotonic, sleep=time.sleep, cancelled=None):
        self.config, self.cache_dir = config, Path(cache_dir)
        self.max_requests = max_requests
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.cancelled = cancelled or (lambda: False)
        self.requests, self.hits = 0, 0
        self.read_cache = True
        self.last_request = None
        # Namespace a local cache by a one-way credential fingerprint, never key text.
        self.partition = sha256(config.api_key.encode()).hexdigest()[:24]
        self.rate_file = self.cache_dir / self.partition / 'last-request.json'
        # inspect/collect both hold collection_lock. Preserve spacing across
        # sequential workers as well as retries within one worker.
        self.previous_wall_time = None
        try:
            stamp = datetime.fromisoformat(json.loads(self.rate_file.read_text(encoding='utf-8'))['at'])
            if stamp.tzinfo is not None and timedelta(0) <= clock() - stamp <= timedelta(minutes=2):
                self.previous_wall_time = stamp
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def checkpoint(self):
        if self.cancelled():
            raise MassiveError('cancelled', '下载已停止；已缓存的数据可供下次继续。')

    def _wait(self, delay):
        while delay > 0:
            self.checkpoint()
            step = min(delay, 0.25)
            self.sleep(step)
            delay -= step

    def request(self, path, params=None, *, cache_seconds=86400):
        self.checkpoint()
        url = canonical_url(path + ('?' + urlencode(params) if params else ''))
        # Request identity cannot contain credential material, including a hostile cursor.
        if self.config.api_key in url:
            raise MassiveError('unsafe_url', '分页地址包含异常凭据，下载已停止。')
        now = self.clock()
        filename = self.cache_dir / self.partition / (sha256(url.encode()).hexdigest() + '.json')
        if self.read_cache and cache_seconds > 0 and filename.is_file():
            try:
                cached = json.loads(filename.read_text(encoding='utf-8'))
                stamp = datetime.fromisoformat(cached['observed_at'])
                if (cached['request'] == url and stamp.tzinfo is not None
                        and timedelta(0) <= now - stamp < timedelta(seconds=cache_seconds)
                        and isinstance(cached['data'], dict)
                        and cached['data'].get('status', 'OK') in {'OK', 'DELAYED'}):
                    checksum = cached.get('content_sha256')
                    if ((checksum is not None and checksum != _payload_digest(cached['data']))
                            or ('/v2/aggs/grouped/' in url and checksum is None)):
                        raise ValueError('Invalid cache checksum')
                    if cached['data'].get('next_url'):
                        canonical_url(cached['data']['next_url'], expected_path=urlsplit(url).path)
                    self.hits += 1
                    return cached
            except (ValueError, KeyError, TypeError):
                pass
        interval = 60 / self.config.requests_per_minute + 0.05
        for attempt in range(3):
            self.checkpoint()
            if self.requests >= self.max_requests:
                raise MassiveError('budget', '本轮请求数已达上限；缓存已保留，下次可继续。')
            if self.last_request is not None:
                self._wait(self.last_request + interval - self.monotonic())
            elif self.previous_wall_time is not None:
                self._wait(interval - (self.clock() - self.previous_wall_time).total_seconds())
            self.last_request = self.monotonic()
            write_json(self.rate_file, {'at': self.clock().isoformat()})
            self.requests += 1
            request = Request(url, headers={'Authorization': 'Bearer ' + self.config.api_key,
                                          'Accept': 'application/json', 'User-Agent': 'TradingBot/1.0'})
            try:
                with self.opener.open(request, timeout=20) as response:
                    if response.geturl() != url:
                        raise MassiveError('redirect', 'Massive 请求发生重定向，已拒绝。')
                    content = response.read(20_000_001)
                    if len(content) > 20_000_000:
                        raise MassiveError('response_size', 'Massive 单页数据超出大小限制。')
                    payload = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(payload, dict) or payload.get('status', 'OK') not in {'OK', 'DELAYED'}:
                    raise MassiveError('provider_status', 'Massive 没有返回成功的数据响应。')
                if 'results' not in payload or not isinstance(payload['results'], (list, dict)):
                    # An empty bar response may omit results; counts must explicitly be zero.
                    if payload.get('resultsCount') == 0:
                        payload['results'] = []
                    else:
                        raise MassiveError('shape', 'Massive 数据结构不符合接口约定。')
                if payload.get('next_url'):
                    payload['next_url'] = canonical_url(payload['next_url'], expected_path=urlsplit(url).path)
                payload = {k: v for k, v in payload.items() if k in {
                    'results', 'next_url', 'ticker', 'adjusted', 'count', 'queryCount', 'resultsCount', 'status'}}
                saved = {'request': url, 'observed_at': self.clock().isoformat(),
                         'data': _scrub(payload, self.config.api_key)}
                saved['content_sha256'] = _payload_digest(saved['data'])
                if cache_seconds > 0:
                    write_json(filename, saved)
                return saved
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                exc.close()
                if status == 401:
                    raise MassiveError('authentication', 'Massive API Key 无效或已过期，请重新配置。') from None
                if status == 403:
                    raise MassiveError('entitlement', 'Massive 套餐未授权此数据；请检查日线及 Financials & Ratios 权限。') from None
                if status == 404:
                    raise MassiveError('not_found', 'Massive 没有找到该股票的数据。') from None
                if status == 429 or 500 <= status <= 599:
                    if attempt < 2:
                        delay = max(interval, 2 ** attempt)
                        if retry_after.isdigit():
                            if int(retry_after) > 60:
                                raise MassiveError('rate_limit', 'Massive 要求较长等待；请稍后继续下载。') from None
                            delay = max(delay, int(retry_after))
                        self._wait(delay)
                        continue
                raise MassiveError('http_' + str(status), f'Massive 请求失败（HTTP {status}），请检查权限或稍后重试。') from None
            except (URLError, TimeoutError, OSError):
                if attempt < 2:
                    self._wait(2 ** attempt)
                    continue
                raise MassiveError('network', 'Massive 网络连接失败，缓存已保留。') from None
            except (ValueError, UnicodeError) as exc:
                if isinstance(exc, MassiveError):
                    raise
                raise MassiveError('invalid_json', 'Massive 返回的数据无法解析。') from None

    def pages(self, path, params=None, *, max_pages=100, max_rows=50000, cache_seconds=86400):
        url = canonical_url(path + ('?' + urlencode(params) if params else ''))
        expected_path = urlsplit(url).path
        seen, rows, observations = set(), [], []
        for _ in range(max_pages):
            url = canonical_url(url, expected_path=expected_path)
            if url in seen:
                raise MassiveError('pagination_cycle', 'Massive 分页出现重复，未采用不完整数据。')
            seen.add(url)
            saved = self.request(url, cache_seconds=cache_seconds)
            batch = saved['data']['results']
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise MassiveError('shape', 'Massive 列表数据格式错误。')
            rows.extend(batch)
            observations.append(saved['observed_at'])
            if len(rows) > max_rows:
                raise MassiveError('row_limit', 'Massive 数据超过设定条数，未截断股票池。')
            url = saved['data'].get('next_url')
            if not url:
                return {'results': rows, 'observed_at': max(observations),
                        'oldest_observed_at': min(observations)}
        raise MassiveError('page_limit', 'Massive 分页超过设定上限，未采用不完整数据。')

    def daily(self, ticker, start, end, *, adjusted=True):
        if not TICKER.fullmatch(ticker):
            raise MassiveError('ticker', '股票代码格式无效。')
        path = f'/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}'
        saved = self.request(path, {'adjusted': str(adjusted).lower(), 'sort': 'asc', 'limit': 50000})
        raw = saved['data']
        if raw.get('ticker') != ticker or raw.get('adjusted') is not adjusted or raw.get('next_url'):
            raise MassiveError('bar_identity', 'Massive 日线代码、复权口径或完整性校验失败。')
        return saved

    def cache_file(self, path, params=None):
        url = canonical_url(path + ('?' + urlencode(params) if params else ''))
        return self.cache_dir / self.partition / (sha256(url.encode()).hexdigest() + '.json')

    def invalidate(self, path, params=None):
        """Drop one cached response so a malformed payload is not served again."""
        try:
            self.cache_file(path, params).unlink(missing_ok=True)
        except OSError:
            pass

    GROUPED_PARAMS = {'adjusted': 'false', 'include_otc': 'false'}

    def invalidate_grouped(self, day):
        self.invalidate(f'/v2/aggs/grouped/locale/us/market/stocks/{day}', self.GROUPED_PARAMS)

    def grouped_daily(self, day, *, cache_seconds=86400):
        """One completed day's raw US market; cache identity is the trading date."""
        path = f'/v2/aggs/grouped/locale/us/market/stocks/{day}'
        saved = self.request(path, self.GROUPED_PARAMS, cache_seconds=cache_seconds)
        raw = saved['data']
        if (raw.get('adjusted') is not False or raw.get('next_url')
                or not isinstance(raw.get('results'), list)
                or type(raw.get('resultsCount')) is not int or raw['resultsCount'] != len(raw['results'])):
            self.invalidate(path, self.GROUPED_PARAMS)
            raise MassiveError('grouped_identity', 'Massive 批量日线的复权口径或完整性校验失败；已清除该日缓存。')
        return saved
