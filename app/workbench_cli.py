"""CLI client of the running workbench; shares selections, jobs and execution gates."""
import argparse
from getpass import getpass
from http.cookiejar import CookieJar
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, Request, build_opener

from app.control_contracts import ControlError
from app.operations import WORKBENCH_ACTIONS as ACTIONS
from app.origins import validate_origin

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class WorkbenchClient:
    def __init__(self, root=ROOT, url=None):
        self.instance = None
        if url is None:
            try:
                saved = json.loads((Path(root) / 'runs/portal/server.json').read_text(encoding='utf-8'))
                port = saved['port']
                if type(port) is not int or not 1 <= port <= 65535:
                    raise ValueError('port')
                url, self.instance = f'http://127.0.0.1:{port}', saved['instance']
            except (OSError, ValueError, KeyError, TypeError):
                raise ControlError('请先启动工作台；服务器可用 --url 指定工作台的 HTTPS 地址。') from None
        self.url = validate_origin(url)
        self.token = None
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(CookieJar()))

    def request(self, path, data=None):
        headers = {'Origin': self.url, 'Accept': 'application/json'}
        if self.token:
            headers['X-Portal-Token'] = self.token
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.url + path, headers=headers,
                          data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.opener.open(request, timeout=40) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                message = json.load(exc).get('message', '工作台拒绝了此操作。')
            except (ValueError, AttributeError):
                message = '工作台拒绝了此操作，请核对地址、登录和当前状态。'
            raise ControlError(message) from None
        except (URLError, OSError, ValueError):
            raise ControlError('未取得工作台响应；操作不会自动重试，请在面板核对任务记录。') from None

    def connect(self, password_prompt=getpass):
        health = self.request('/health')
        if (health.get('application') != 'trading-bot-portal'
                or self.instance and health.get('instance') != self.instance):
            raise ControlError('地址不属于当前工作台实例。')
        auth = self.request('/api/auth/status')
        if auth['enabled'] and not auth['authenticated']:
            self.token = auth['login_token']
            username = input('工作台用户名 [owner]: ').strip() or 'owner'
            self.request('/api/auth/login', {'username': username, 'password': password_prompt('工作台密码: ')})
        self.token = self.request('/api/session')['token']
        return self

    def operate(self, action, confirmation=None):
        if action not in ACTIONS - {'configure-data'}:
            raise ControlError('未知工作台操作。')
        state = self.request('/api/state')
        allowed = state['actions'][action]
        if not allowed['enabled']:
            raise ControlError(allowed['message'])
        payload = {'action': action}
        if action in {'execute', 'resolve-remainder'}:
            if not confirmation or confirmation != state['broker']['id']:
                raise ControlError('请先查看订单预览，再用 --confirm-broker 明确确认当前模拟券商编号。')
            payload['confirmation'] = confirmation
        # Exactly one POST. A timeout must never cause an automatic repeat order.
        return self.request('/api/jobs', payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=sorted((ACTIONS - {'configure-data'}) | {'view', 'orders', 'broker', 'source'}))
    parser.add_argument('--url', help='Running Portal HTTPS URL or local desktop URL')
    parser.add_argument('--id', help='Registered broker or data instance id')
    parser.add_argument('--confirm-broker', help='Exact broker id displayed with the reviewed order preview')
    args = parser.parse_args(argv)
    try:
        client = WorkbenchClient(url=args.url).connect()
        if args.action in {'view', 'orders'}:
            result = client.request('/api/' + ('state' if args.action == 'view' else 'orders'))
        elif args.action in {'broker', 'source'}:
            if not args.id:
                parser.error('--id is required for module selection')
            result = client.request('/api/' + args.action, {args.action: args.id})
        else:
            result = client.operate(args.action, args.confirm_broker)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ControlError as exc:
        print(str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
