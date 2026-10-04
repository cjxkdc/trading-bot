"""Official moomoo REST OAuth and simulation-account verification; read-only pilot.

This account namespace is independent from the existing OpenD adapter. Until its
order lifecycle is verified, this module exposes no order/preparation capability.
"""
import base64
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from app.control_contracts import ControlError
from brokers.controls import BrokerControl
from app.credential_store import ActiveVault

HOST = 'https://webapi.moomoo.com'
TYPE = 'moomoo_oauth_paper'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ControlError('券商接口重定向已阻止，请核对官方接口。')


def request(method, path, *, payload=None, token=None, form=False):
    # Only authorization and simulated-account endpoints are reachable here.
    if (method, path) not in {('POST', '/oauth2/register'), ('POST', '/oauth2/token'),
                             ('GET', '/api/v1.0/sim-trade/accounts')}:
        if method != 'GET' or not re.fullmatch(r'/api/v1\.0/sim-trade/[0-9]{1,20}/(cash-info|positions|orders)', path):
            raise ControlError('网页授权测试模块只允许读取模拟账户。')
    headers, data = {'Accept': 'application/json'}, None
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if payload is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = urlencode(payload).encode() if form else json.dumps(payload).encode()
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
                Request(HOST + path, data=data, headers=headers, method=method), timeout=20) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('size')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('shape')
        return value
    except Exception:
        raise ControlError('券商网页接口未返回有效结果，请重新检查授权或账户权限。') from None


def data_reply(value):
    if type(value.get('ret_code')) is not int or value['ret_code'] != 0 or not isinstance(value.get('data'), dict):
        raise ControlError('券商没有确认模拟账户请求成功。')
    return value['data']


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError('amount')
        return result
    except (ValueError, InvalidOperation):
        raise ControlError('模拟账户金额或数量无效。') from None


class MoomooAuthorization:
    def __init__(self, root, broker_id=TYPE, transport=request, clock=time.time):
        self.vault, self.broker_id = ActiveVault(root), broker_id
        self.transport, self.clock = transport, clock
        self.pending = None

    def begin(self, callback):
        from app.origins import validate_origin
        parsed = urlsplit(callback)
        validate_origin(parsed.scheme + '://' + parsed.netloc)
        if parsed.path != '/api/broker-auth/callback' or parsed.query or parsed.fragment:
            raise ControlError('网页授权回调地址无效。')
        self.pending = None
        self.vault.clear()
        registration = self.transport('POST', '/oauth2/register', payload={
            'redirect_uris': [callback], 'token_endpoint_auth_method': 'none',
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'client_name': 'Trading Bot - Simulation Account Verification'})
        client_id = registration.get('client_id')
        if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', client_id):
            raise ControlError('券商没有返回有效的应用注册信息。')
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.pending = {'client_id': client_id, 'verifier': verifier, 'state': state,
                        'callback': callback, 'expires': self.clock() + 300}
        query = urlencode({'client_id': client_id, 'code_challenge': challenge, 'code_challenge_method': 'S256',
                           'redirect_uri': callback, 'response_type': 'code', 'state': state,
                           'scope': 'trade:read'})
        return {'authorization_url': HOST + '/oauth2/authorize/confirm?' + query,
                'message': '请在券商官网授权读取模拟账户；首次读取可能初始化官网沙盒账户。'}

    def finish(self, state, code):
        pending, self.pending = self.pending, None  # Consume once even if the exchange fails.
        if (not pending or self.clock() > pending['expires'] or not isinstance(state, str)
                or not secrets.compare_digest(state.encode(), pending['state'].encode())
                or not isinstance(code, str) or not 1 <= len(code) <= 2048):
            raise ControlError('授权回调已过期或不属于当前账户模块，请重新开始。')
        value = self.transport('POST', '/oauth2/token', form=True, payload={
            'grant_type': 'authorization_code', 'code': code, 'client_id': pending['client_id'],
            'redirect_uri': pending['callback'], 'code_verifier': pending['verifier']})
        session = self._token(value, pending['client_id'])
        session.update(accounts={}, selected=None)
        self.vault.save(self.broker_id, session)
        return self.accounts()

    def _token(self, value, client_id, previous=None):
        access, refresh, seconds, scopes = (value.get(k) for k in ('access_token', 'refresh_token', 'expires_in', 'scope'))
        if (not isinstance(access, str) or not 1 <= len(access) <= 8192
                or value.get('token_type', '').lower() != 'bearer' or type(seconds) is not int or not 30 <= seconds <= 86400
                or not isinstance(scopes, str) or 'trade:read' not in scopes.split()
                or 'trade:write' in scopes.split()):
            raise ControlError('此测试模块只接受账户读取授权，请在官网取消交易写入权限后重试。')
        refresh = refresh or (previous or {}).get('refresh_token')
        if refresh is not None and (not isinstance(refresh, str) or len(refresh) > 8192):
            raise ControlError('券商授权刷新信息无效。')
        return {'client_id': client_id, 'access_token': access, 'refresh_token': refresh,
                'expires_at': self.clock() + seconds, 'scope': scopes}

    def session(self):
        value = self.vault.load(self.broker_id)
        if value is None:
            raise ControlError('请先通过券商官网完成网页授权。')
        if value['expires_at'] <= self.clock() + 30:
            if not value.get('refresh_token'):
                raise ControlError('网页授权已过期，请重新授权。')
            reply = self.transport('POST', '/oauth2/token', form=True, payload={
                'grant_type': 'refresh_token', 'refresh_token': value['refresh_token'], 'client_id': value['client_id']})
            value.update(self._token(reply, value['client_id'], value))
            self.vault.save(self.broker_id, value)
        return value

    def accounts(self):
        value = self.session()
        rows = data_reply(self.transport('GET', '/api/v1.0/sim-trade/accounts', token=value['access_token'])).get('accounts')
        if not isinstance(rows, list) or len(rows) > 100:
            raise ControlError('模拟账户列表不完整。')
        accounts, seen = {}, set()
        for row in rows:
            if (not isinstance(row, dict) or not re.fullmatch(r'[0-9]{1,20}', str(row.get('account_id', '')))
                    or str(row['account_id']) in seen):
                raise ControlError('模拟账户列表中的标识无效或重复。')
            seen.add(str(row['account_id']))
            # This is the dedicated simulation endpoint; never query real accounts.
            if row.get('market_id') != 2:
                continue
            ident = sha256((value['client_id'] + ':' + str(row['account_id'])).encode()).hexdigest()[:24]
            accounts[ident] = {'account_id': str(row['account_id']), 'market_id': 2,
                               'title': str(row.get('account_title', 'US Simulation'))[:120]}
        value['accounts'] = accounts
        if value.get('selected') not in accounts:
            value['selected'] = None
        self.vault.save(self.broker_id, value)
        return self.public()

    def bind(self, ident):
        value = self.session()
        if not isinstance(ident, str) or ident not in value.get('accounts', {}):
            raise ControlError('请选择官网返回的美股模拟账户。')
        value['selected'] = ident
        self.vault.save(self.broker_id, value)
        return self.public()

    def public(self):
        value = self.vault.load(self.broker_id)
        return {'mode': 'oauth', 'authorized': value is not None,
                'expires_at': value.get('expires_at') if value else None, 'selected': value.get('selected') if value else None,
                'accounts': [{'id': ident, 'name': row['title'], 'account_hint': '***' + row['account_id'][-4:],
                              'environment': 'SIMULATE'} for ident, row in (value or {}).get('accounts', {}).items()],
                'order_access': False,
                'note': '独立的网页沙盒账户，只读验证；与 OpenD 模拟账户不自动合并。'}

    def disconnect(self):
        self.pending = None
        self.vault.clear()
        return {'message': '本程序的网页授权已清除；如需撤销券商端授权，请在券商官网操作。'}


class MoomooOAuthControl(BrokerControl):
    def config_revision(self):
        return ActiveVault(self.root).revision(getattr(self, 'instance_id', TYPE))

    def inspect(self, *, connect=False):
        auth = MoomooAuthorization(self.root, getattr(self, 'instance_id', TYPE))
        if not connect:
            return {'status': 'ready' if auth.public()['authorized'] else 'not_ready',
                    'issues': [] if auth.public()['authorized'] else ['请先进行券商网页授权。'],
                    'network_requested': False}
        value = auth.session()
        selected = value.get('accounts', {}).get(value.get('selected'))
        if not selected or selected['market_id'] != 2:
            raise ControlError('请先选择官网返回的美股模拟账户。')
        account_id = selected['account_id']
        get = lambda suffix: data_reply(auth.transport('GET', '/api/v1.0/sim-trade/' + account_id + '/' + suffix,
                                                     token=value['access_token']))
        cash, positions, orders = get('cash-info'), get('positions'), get('orders')
        rows = positions.get('positions')
        states = orders.get('orders')
        if not isinstance(rows, list) or not isinstance(states, list) or len(rows) > 500 or len(states) > 5000:
            raise ControlError('模拟账户的持仓或委托列表无法完整核对。')
        held, symbols = [], set()
        for row in rows:
            symbol = row.get('symbol', '')
            qty, available = number(row.get('qty')), number(row.get('qty_avbl'))
            if (row.get('pstn_type') != 0 or row.get('market') != 2 or not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', symbol)
                    or qty != qty.to_integral_value() or available != available.to_integral_value()
                    or available > qty or symbol in symbols):
                raise ControlError('账户包含本模块无法核验的持仓，请在官网检查。')
            symbols.add(symbol)
            held.append({'symbol': 'US.' + symbol, 'quantity': str(qty), 'sellable': str(available)})
        order_ids, open_count = set(), 0
        for row in states:
            ident = str(row.get('order_id', ''))
            if not ident or ident in order_ids or row.get('status') not in {2, 3, 4, 5, 6}:
                raise ControlError('模拟委托状态未知，请在官网核对。')
            order_ids.add(ident)
            open_count += row['status'] in {2, 3}
        balance, frozen = number(cash.get('balance')), number(cash.get('hold'))
        return {'status': 'connected', 'issues': [], 'network_requested': True,
                'account_environment': 'SIMULATE', 'currency': 'USD', 'cash': str(max(Decimal(0), balance - frozen)),
                'positions': held, 'open_order_count': open_count,
                'account_fingerprint': sha256(('moomoo-rest-simulation:' + account_id).encode()).hexdigest(),
                'order_access': False, 'message': '网页沙盒账户读取通过；此接口当前不提供下单。'}

    def session(self, *args, **kwargs):
        raise ControlError('网页沙盒测试模块未启用交易执行。')


class MoomooOAuthConnector:
    mode = 'broker_paper'
    def __init__(self, **kwargs):
        pass
    def connect(self, *args, **kwargs):
        raise ControlError('网页沙盒测试模块只提供账户读取。')
