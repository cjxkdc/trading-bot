"""Single-owner remote Portal authentication, separate from broker authorization."""
from collections import deque
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
import hmac
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from app.control_contracts import ControlError

ITERATIONS = 600_000
COOKIE = 'trading_portal_session'


from app.origins import validate_origin, validate_server_origin


def password_record(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ControlError('工作台密码需要 12–256 个字符。')
    salt = secrets.token_hex(16)
    return {'algorithm': 'pbkdf2_sha256', 'iterations': ITERATIONS, 'salt': salt,
            'digest': pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()}


@dataclass(frozen=True)
class RemoteSettings:
    origin: str
    password: dict
    username: str = 'owner'
    session_seconds: int = 8 * 3600

    def __post_init__(self):
        validate_server_origin(self.origin)

    @classmethod
    def load(cls, root):
        path = Path(root) / 'secrets/portal/server.json'
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            return cls.from_record(raw)
        except (OSError, ValueError, TypeError, KeyError):
            raise ControlError('请先配置服务器 Portal 的 HTTPS 地址和登录密码。') from None

    @classmethod
    def from_record(cls, raw):
        try:
            if set(raw) != {'origin', 'username', 'password', 'session_seconds'}:
                raise ValueError('fields')
            origin = validate_server_origin(raw['origin'])
            username, seconds, record = raw['username'], raw['session_seconds'], raw['password']
            if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', username):
                raise ValueError('user')
            if type(seconds) is not int or not 300 <= seconds <= 86400:
                raise ValueError('ttl')
            if (not isinstance(record, dict) or set(record) != {'algorithm', 'iterations', 'salt', 'digest'}
                    or record['algorithm'] != 'pbkdf2_sha256' or record['iterations'] != ITERATIONS
                    or not re.fullmatch(r'[a-f0-9]{32}', record['salt'])
                    or not re.fullmatch(r'[a-f0-9]{64}', record['digest'])):
                raise ValueError('password')
        except (ValueError, TypeError, KeyError):
            raise ControlError('请先配置服务器 Portal 的地址和登录密码。') from None
        return cls(origin, record, username, seconds)


class OwnerSessions:
    def __init__(self, settings, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.sessions, self.attempts = {}, deque()
        self.lock = threading.Lock()

    @property
    def secure(self):
        return self.settings.origin.startswith('https://')

    def authenticated(self, cookie):
        with self.lock:
            stamp = self.clock()
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > stamp}
            return bool(cookie) and self.sessions.get(cookie, 0) > stamp

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
            raise ControlError('用户名或密码不正确。')
        # Global bound as well as TLS/proxy restrictions; an untrusted forwarding header
        # cannot bypass throttling. The single-owner Portal is not a multi-tenant product.
        with self.lock:
            stamp = self.clock()
            while self.attempts and self.attempts[0] <= stamp - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 8:
                raise ControlError('登录尝试过多，请稍后重试。')
            self.attempts.append(stamp)
        record = self.settings.password
        actual = pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(record['salt']), record['iterations']).hex()
        valid = hmac.compare_digest(actual, record['digest']) & hmac.compare_digest(username.encode(), self.settings.username.encode())
        if not valid:
            raise ControlError('用户名或密码不正确。')
        with self.lock:
            if len(self.sessions) >= 8:
                oldest = min(self.sessions, key=self.sessions.get)
                self.sessions.pop(oldest, None)
            cookie = secrets.token_urlsafe(48)
            self.sessions[cookie] = self.clock() + self.settings.session_seconds
            return cookie

    def logout(self, cookie):
        with self.lock:
            self.sessions.pop(cookie, None)


class RemoteOnly:
    """Exact configured Host/Origin, cookie authentication, and mutation CSRF token."""
    def __init__(self, app, *, access, token):
        self.app, self.access, self.token = app, access, token
        self.origin = access.settings.origin
        self.host = urlsplit(self.origin).netloc

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from http.cookies import SimpleCookie
        from fastapi.responses import JSONResponse
        from portal.service import MAX_FILE
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        origin = headers.get(b'origin', b'').decode('latin1')
        site = headers.get(b'sec-fetch-site', b'').decode('latin1')
        mutation = scope['method'] not in {'GET', 'HEAD', 'OPTIONS'}
        path = scope['path']
        error = None
        repeated = any(sum(name == key for name, _ in scope['headers']) > 1
                       for key in (b'host', b'origin', b'x-portal-token', b'content-length'))
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get(b'cookie', b'').decode('latin1'))
            session = cookie[COOKIE].value if COOKIE in cookie else ''
        except Exception:
            session = ''
        if repeated or host != self.host or (origin and origin != self.origin):
            error = (403, '此地址不属于已配置的服务器工作台。')
        elif site not in {'', 'same-origin', 'none'} and path != '/api/broker-auth/callback':
            error = (403, '不允许跨网站访问工作台。')
        elif mutation and (origin != self.origin or not secrets.compare_digest(
                headers.get(b'x-portal-token', b''), self.token.encode())):
            error = (403, '工作台会话已更新，请刷新页面。')
        elif path.startswith('/api/') and path not in {'/api/auth/status', '/api/auth/login', '/api/broker-auth/callback'}:
            if not self.access.authenticated(session):
                error = (401, '请先登录服务器工作台。')
            elif path != '/api/session' and not secrets.compare_digest(headers.get(b'x-portal-token', b''), self.token.encode()):
                error = (403, '工作台会话已更新，请刷新页面。')
        if mutation and not error:
            try:
                limit = 4096 if path == '/api/auth/login' else MAX_FILE
                if int(headers.get(b'content-length', b'0')) > limit:
                    raise ValueError('size')
                data = bytearray()
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    data.extend(message.get('body', b''))
                    if len(data) > limit:
                        raise ValueError('size')
                    if not message.get('more_body', False):
                        break
                sent, original = False, receive

                async def buffered():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {'type': 'http.request', 'body': bytes(data), 'more_body': False}
                    return await original()
                receive = buffered
            except ValueError:
                error = (413, '请求超过读取上限。')

        async def secured(message):
            if message['type'] == 'http.response.start':
                message.setdefault('headers', []).extend([
                    (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    (b'referrer-policy', b'no-referrer'), (b'x-frame-options', b'DENY'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")])
            await send(message)
        if error:
            return await JSONResponse({'message': error[1]}, status_code=error[0])(scope, receive, secured)
        return await self.app(scope, receive, secured)
